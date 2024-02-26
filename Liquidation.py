import json
import requests
import time
import datetime
import os
from dataclasses import dataclass

from web3 import Web3, Account
from web3.middleware import geth_poa_middleware
from dotenv import load_dotenv


load_dotenv('.env')

CURVE_TRADING_FEE_DECIMALS = 10

# This RPC should ideally be localhost
rpc_url = os.getenv('RPC')
web3 = Web3(Web3.WebsocketProvider(rpc_url))

password = os.getenv('PASSWORD')
deployment_block = int(os.getenv('DEPLOYMENT_BLOCK'))

graph_url = os.getenv('SUBGRAPH_URL')

# Required to make compatible with Rinkeby testnet
if web3.eth.chain_id == 4:
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

contract_details_folder = f'''deployments/{os.getenv('NETWORK')}'''

# Load in contracts we will interact with
with open(f'{contract_details_folder}/clearinghouse.json', 'r') as clearinghouse_json:
    clearinghouse = json.load(clearinghouse_json)
clearinghouse_contract = web3.eth.contract(address=clearinghouse['address'], abi=clearinghouse['abi'])

with open(f'{contract_details_folder}/clearingHouseViewer.json', 'r') as clearinghouse_viewer_json:
    clearinghouse_viewer = json.load(clearinghouse_viewer_json)
clearinghouse_viewer_contract = web3.eth.contract(address=clearinghouse_viewer['address'], abi=clearinghouse_viewer['abi'])

with open(f'{contract_details_folder}/vault.json', 'r') as vault_json:
    vault = json.load(vault_json)
vault_contract = web3.eth.contract(address=vault['address'], abi=vault['abi'])

# Setup wallet from keyfile
with open(f'./{os.getenv("KEYFILE")}') as keyfile:
    account = Account.from_key(web3.eth.account.decrypt(keyfile.read(), password))
    print(f'Password accepted, using account {account.address}')

with open(f'{contract_details_folder}/perp.json', 'r') as perp_json:
    perp_abi = json.load(perp_json)['abi']

with open(f'{contract_details_folder}/market.json', 'r') as market_json:
    market_abi = json.load(market_json)['abi']

if not os.path.isfile('state.json'):
    with open('state.json', 'x') as f:
        start_state = {
            'synced_block': deployment_block,
            'perps': {},
            'trader_positions': {},
            'reserves': {},
            'reserve_weights': {},
            'ua_address': vault_contract.functions.UA().call()
        }
        f.write(json.dumps(start_state))

with open('state.json', 'r') as f:
    state = json.loads(f.read())


transaction_dict = {
    'chainId': web3.eth.chain_id,
    'nonce': web3.eth.get_transaction_count(account.address),
    'gasPrice': 2*web3.eth.gas_price,
    'gas': 3*(10**6)
}


@dataclass
class TraderPosition:
    idx: int
    user: str
    open_notional: int
    position_size: int


@dataclass
class UserDebtPosition:
    user: str
    ua_balance: int


### SYNC FUNCTIONS ###

def sync_clearinghouse_parameters(to_block):
    logs = clearinghouse_contract.events.ClearingHouseParametersChanged.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        state['min_margin'] = args['newMinMargin']
        state['ua_debt_seizure_threshold'] = args['uaDebtSeizureThreshold']
        state['non_ua_coll_seizure_discount'] = args['nonUACollSeizureDiscount']

def sync_collateral_weights(to_block):
    logs = vault_contract.events.CollateralAdded.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        asset = args['asset']
        weight = args['weight']
        state['reserve_weights'][asset] = weight

    logs = vault_contract.events.CollateralWeightChanged.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        asset = args['asset']
        weight = args['newWeight']
        state['reserve_weights'][asset] = weight


def sync_markets_added(to_block):
    logs = clearinghouse_contract.events.MarketAdded.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        idx = str(args['listedIdx'])
        perp_address = clearinghouse_contract.functions.perpetuals(int(idx)).call()
        market_address = web3.eth.contract(address=perp_address, abi=perp_abi).functions.market().call()
        market_out_fee = web3.eth.contract(address=market_address, abi=market_abi).functions.out_fee().call()
        # INDEX PRICE SHOULD NOT BE HERE
        index_price = web3.eth.contract(address=perp_address, abi=perp_abi).functions.indexPrice().call()
        risk_weight = web3.eth.contract(address=perp_address, abi=perp_abi).functions.riskWeight().call()

        state['trader_positions'][idx] = {}
        state['perps'][idx] = {
            'address': perp_address,
            'market_out_fee': market_out_fee,
            'index_price': index_price,
            'risk_weight': risk_weight
        }

def sync_markets_removed(to_block):
    logs = clearinghouse_contract.events.MarketRemoved.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        idx = str(args['delistedIdx'])
        del state['trader_positions'][idx]
        del state['perps'][idx]

def sync_deposits(to_block):
    logs = vault_contract.events.Deposit.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        user = args['user']
        asset = args['asset']
        amount = args['amount']

        if user not in state['reserves']:
            state['reserves'][user] = {}

        if asset not in state['reserves'][user]:
            state['reserves'][user][asset] = 0

        state['reserves'][user][asset] += amount

def sync_withdrawals(to_block):
    logs = vault_contract.events.Withdraw.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        user = args['user']
        asset = args['asset']
        amount = args['amount']

        state['reserves'][user][asset] -= amount

def sync_funding(to_block):
    for idx in state['perps']:
        perp_address = state['perps'][idx]['address']
        perp_contract = web3.eth.contract(address=perp_address, abi=perp_abi)
        logs = perp_contract.events.FundingPaid.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
        for event_log in logs:
            args = event_log['args']
            account = args['account']
            amount = args['amount']

            if account not in state['reserves']:
                state['reserves'][account] = {}

            if state['ua_address'] not in state['reserves'][account]:
                state['reserves'][account][state['ua_address']] = 0

            state['reserves'][account][state['ua_address']] += amount


# Updates the state trader positions from the state synced_block to to_block
def sync_trader_positions(to_block):
    logs = clearinghouse_contract.events.ChangePosition.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        idx = str(args['idx'])
        user = args['user']
        added_open_notional = args['addedOpenNotional']
        added_position_size = args['addedPositionSize']
        is_position_closed = args['isPositionClosed']
        is_position_increased = args['isPositionIncreased']
        profit = args['profit']
        trading_fees_payed = args['tradingFeesPayed']

        state['reserves'][user][state['ua_address']] += profit

        if not is_position_increased:
            added_open_notional -= profit + trading_fees_payed

        if user not in state['trader_positions'][idx]:
            state['trader_positions'][idx][user] = {
                'open_notional': 0,
                'position_size': 0
            }

        state['trader_positions'][idx][user]['open_notional'] += added_open_notional
        state['trader_positions'][idx][user]['position_size'] += added_position_size

        if is_position_closed:
            del state['trader_positions'][idx][user]



### HELPER FUNCTIONS ###

# STILL NEEDS LP PNL DONE
def get_pnl_across_markets(trader):
    trader_pnl = 0
    lp_pnl = 0
    for idx in state['perps']:
        oracle_price = state['perps'][idx]['index_price']
        position_size = state['trader_positions'][idx][trader]['position_size']
        open_notional = state['trader_positions'][idx][trader]['open_notional']

        v_quote_virtual_proceeds = int(oracle_price / (10**18) * position_size)
        fees_in_wad = state['perps'][idx]['market_out_fee'] * 10**(18 - CURVE_TRADING_FEE_DECIMALS)
        trading_fees = int(abs(v_quote_virtual_proceeds) / (10**18) * fees_in_wad)

        trader_pnl += open_notional + v_quote_virtual_proceeds - trading_fees
        
    return trader_pnl + lp_pnl

# STILL NEEDS LP DEBT DONE
def get_debt_across_markets(trader):
    trader_debt = 0
    lp_debt = 0
    for idx in state['perps']:
        oracle_price = state['perps'][idx]['index_price']
        risk_weight = state['perps'][idx]['risk_weight']
        position_size = state['trader_positions'][idx][trader]['position_size']
        open_notional = state['trader_positions'][idx][trader]['open_notional']

        quote_debt = min(open_notional, 0)
        base_debt = min(int(position_size / (10**10) * oracle_price), 0)

        market_trader_debt = abs(quote_debt + base_debt)
        trader_debt += int(market_trader_debt / (10**18) * risk_weight)

    return trader_debt + lp_debt

# THIS NEEDS TO BE IMPLEMENTED BETTER FOR MULTI COLLATERAL. SHOULD BE oracle.getPrice() FUNCTION. WILL ONLY WORK FOR UA
def get_oracle_price(token_address, token_balance):
    if token_address == state['ua_address']:
        return 1 * (10**18)
    else:
        return None

def get_reserve_value(trader):
    reserve_value = 0

    for reserve_token in state['reserves'][trader]:
        balance = state['reserves'][trader][reserve_token]
        if balance != 0:
            weighted_balance = int(balance / (10**18) * state['reserve_weights'][reserve_token])
            usd_per_unit = get_oracle_price(reserve_token, balance)
            reserve_value += int(weighted_balance / (10**18) * usd_per_unit)

    return reserve_value

def get_total_margin_requirement(trader, ratio):
    user_debt = get_debt_across_markets(trader)
    return int(user_debt / (10**18) * ratio)

# SHOULD STILL FACTOR IN PENDING FUNDING PAYMENT
def is_trader_position_valid(trader):
    min_margin = state['min_margin']

    pnl = get_pnl_across_markets(trader)
    total_collateral_value = get_reserve_value(trader)
    margin_required = get_total_margin_requirement(trader, min_margin)

    free_collateral = min(total_collateral_value, total_collateral_value + pnl) - margin_required
    print(free_collateral)
    return free_collateral >= 0


# Submits transaction
def Liquidate_Position(position):
    idx = position.idx
    address = position.user
    is_trader = position.is_trader

    proposed_amount = None
    try:
        if is_trader:
            proposed_amount = clearinghouse_viewer_contract.functions.getTraderProposedAmount(idx, address, int(1e18), 100, 0).call()
            print('Trader')
            #unsigned_tx = clearinghouse_contract.functions.liquidateTrader(idx, address, proposed_amount, 0).buildTransaction(transaction_dict)
        else:
            proposed_amount = clearinghouse_viewer_contract.functions.getLpProposedAmount(idx, address, int(1e18), 100, [0,0]).call()
            print('LP')
            #unsigned_tx = clearinghouse_contract.functions.liquidateLp(idx, address, [0,0], proposed_amount, 0).buildTransaction(transaction_dict)
    except Exception as e:
        print(f'Fail: Position: {clearinghouse_viewer_contract.functions.getTraderPosition(idx, address).call()}')

    print()
    if proposed_amount is not None:

        if is_trader:
            unsigned_tx = clearinghouse_contract.functions.liquidateTrader(idx, address, proposed_amount, 0).buildTransaction(transaction_dict)
        else:
            unsigned_tx = clearinghouse_contract.functions.liquidateLp(idx, address, [0,0], proposed_amount, 0).buildTransaction(transaction_dict)

        signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

        transaction_dict['nonce'] += 1

        return receipt

    return None


def Seize_Collateral(debt_position):
    address = debt_position.user

    unsigned_tx = clearinghouse_contract.functions.seizeCollateral(address).buildTransaction(transaction_dict)

    signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    transaction_dict['nonce'] += 1

    return receipt


def main():
    ## Initialization
    heartbeat = 0
    current_block = web3.eth.block_number

    sync_clearinghouse_parameters(current_block)
    sync_collateral_weights(current_block)
    sync_markets_added(current_block)
    sync_deposits(current_block)
    sync_withdrawals(current_block)
    sync_funding(current_block)
    sync_trader_positions(current_block)
    sync_markets_removed(current_block)
    
    state['synced_block'] = current_block
    with open('state.json', 'w') as f:
        f.write(json.dumps(state))

    # print(get_pnl_across_markets('0x710Af02EEE203a4d6cFa2Cb8cc52A2DA0C0fE809'))
    # print(clearinghouse_contract.functions.getPnLAcrossMarkets('0x710Af02EEE203a4d6cFa2Cb8cc52A2DA0C0fE809').call())

    # real = vault_contract.functions.getReserveValue('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c', True).call()
    # simmed = state['reserves']['0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c'][state['ua_address']]
    # print(real)
    # print(simmed)
    # print(real-simmed)
    #get_reserve_value('0x710Af02EEE203a4d6cFa2Cb8cc52A2DA0C0fE809')
    #2,083.27
    state['perps']['0']['index_price'] = 2500_000000000000000000
    is_trader_position_valid('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c')
    print(clearinghouse_contract.functions.getFreeCollateralByRatio('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c', state['min_margin']).call())

    # print()
    # print('pnl')
    # print(get_pnl_across_markets('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c'))
    # print(clearinghouse_contract.functions.getPnLAcrossMarkets('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c').call())
    # print()

    # print('reserves')
    # print(get_reserve_value('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c'))
    # print(state['reserves']['0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c'][state['ua_address']])
    # print(vault_contract.functions.getReserveValue('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c', True).call())
    # print()
    #print('debt')
    #print(get_debt_across_markets('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c'))
    #orint(clearinghouse_contract.functions.getDebtAcrossMarkets('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c').call())
    # print()
    #print('required margin')
    #print(get_total_margin_requirement('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c', state['min_margin']))
    #print(clearinghouse_contract.functions.getTotalMarginRequirement('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c', state['min_margin']).call())
    # perp_address = clearinghouse_contract.functions.perpetuals(int(0)).call()
    # print(web3.eth.contract(address=perp_address, abi=perp_abi).functions.getUserDebt('0x7342556EF654B12C438a7EBe0a8714fCD139Bc1c').call())

    exit()

    ## Main loop
    while True:
        if heartbeat % 3 == 0:

            debt_position_list = Initialize_User_Debt_Positions()

            print(f'{datetime.datetime.now().strftime("%H:%M:%S")} Currently tracking {len(position_list)} open position(s) and {len(debt_position_list)} UA debt position(s).\n')

        # update index prices
        for perp in state['perps']:
            pass

        # Check if any open positions can be liquidated
        for position in position_list:
            free_collateral = None
            while free_collateral is None:
                try:
                    free_collateral = clearinghouse_contract.functions.getFreeCollateralByRatio(position.user, min_margin).call()
                except:
                    #web3 = Web3(Web3.WebsocketProvider(rpc_url, websocket_timeout=60))
                    time.sleep(10)
            # TODO: Multicall should be used here to group all the marginIsValid() calls, will save seconds if lots of positions are open
            if not free_collateral >= 0:
                print(f'Liquidating user {position.user} on idx {position.idx}.')
                receipt = Liquidate_Position(position)
                #print('Success\n' if receipt.status else 'Fail\n')

        for debt_position in debt_position_list:
            debt = -debt_position.ua_balance
            discounted_collaterals_balance = vault_contract.functions.getReserveValue(debt_position.user, True).call()
            discounted_collaterals_balance_ex_UA = discounted_collaterals_balance - debt_position.ua_balance

            if debt > ua_debt_threshold or debt > (discounted_collaterals_balance_ex_UA * non_UA_coll_seizure_discount) // 10**18:
                print(f'Seizing collateral of user {debt_position.user}.')
                receipt = Seize_Collateral(debt_position)
                #print('Success\n' if receipt.status else 'Fail\n')

        # Ideally this sleep timer is replaced by a block header filter, assumes user has a websocket RPC available
        time.sleep(20)
        heartbeat += 1


if __name__ == '__main__':
    main()
