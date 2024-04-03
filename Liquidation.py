import json
import time
import os
from asyncio.exceptions import TimeoutError

from web3 import Web3, Account
from dotenv import load_dotenv


load_dotenv('.env')

CURVE_TRADING_FEE_DECIMALS = 10
VQUOTE_INDEX = 0
VBASE_INDEX = 1


# This RPC should ideally be localhost
rpc_url = os.getenv('RPC')
web3 = Web3(Web3.WebsocketProvider(rpc_url))

contract_details_folder = f'''deployments/{os.getenv('NETWORK')}'''

# Load in contracts we will interact with
with open(f'{contract_details_folder}/ClearingHouse.json', 'r') as clearinghouse_json:
    clearinghouse = json.load(clearinghouse_json)
clearinghouse_contract = web3.eth.contract(address=clearinghouse['address'], abi=clearinghouse['abi'])

with open(f'{contract_details_folder}/ClearingHouseViewer.json', 'r') as clearinghouse_viewer_json:
    clearinghouse_viewer = json.load(clearinghouse_viewer_json)
clearinghouse_viewer_contract = web3.eth.contract(address=clearinghouse_viewer['address'], abi=clearinghouse_viewer['abi'])

with open(f'{contract_details_folder}/Vault.json', 'r') as vault_json:
    vault = json.load(vault_json)
vault_contract = web3.eth.contract(address=vault['address'], abi=vault['abi'])

# Setup account from private key
account = Account.from_key(os.getenv("PRIVATE_KEY"))
print(f'Using account {account.address}')

with open(f'{contract_details_folder}/Perpetual.json', 'r') as perp_json:
    perp_abi = json.load(perp_json)['abi']

with open(f'{contract_details_folder}/Market.json', 'r') as market_json:
    market_abi = json.load(market_json)['abi']

with open(f'{contract_details_folder}/DeploymentBlock.txt', 'r') as deployment_block_txt:
    deployment_block = int(deployment_block_txt.read())

if not os.path.isfile('state.json'):
    with open('state.json', 'x') as f:
        start_state = {
            'synced_block': deployment_block,
            'perps': {},
            'trader_positions': {},
            'lp_positions': {},
            'global_positions': {},
            'reserves': {},
            'reserve_weights': {},
            'ua_address': vault_contract.functions.UA().call(),
            'liquidation_rewards': 0
        }
        f.write(json.dumps(start_state))

state = {}
perp_contracts = {}
addresses_to_idx = {}

transaction_dict = {
    'chainId': web3.eth.chain_id,
    'nonce': web3.eth.get_transaction_count(account.address),
    'from': account.address,
    'gas': 10*(10**6),
    'maxPriorityFeePerGas': 0,
    'maxFeePerGas': 2*web3.eth.gas_price
}


### SYNC FUNCTIONS ###
def sync(to_block):
    global state

    success = False
    while not success:
        try:
            # reload the state each try to avoid events being processed twice if later events timeout
            with open('state.json', 'r') as f:
                state = json.loads(f.read())

            sync_markets_added(to_block)
            sync_perps()
            sync_all_events(to_block)

            state['synced_block'] = to_block
            with open('state.json', 'w') as f:
                f.write(json.dumps(state))
            success = True

        except TimeoutError:
            print('Timeout error\n')

def sync_all_events(to_block):
    logs = []
    logs.extend(clearinghouse_contract.events.ClearingHouseParametersChanged.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(vault_contract.events.CollateralAdded.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(vault_contract.events.CollateralWeightChanged.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(clearinghouse_contract.events.MarketRemoved.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(vault_contract.events.Deposit.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(vault_contract.events.Withdraw.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(clearinghouse_contract.events.ChangePosition.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(clearinghouse_contract.events.LiquidityProvided.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(clearinghouse_contract.events.LiquidityRemoved.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
    logs.extend(clearinghouse_contract.events.LiquidationCall.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))

    for idx in state['perps']:
        logs.extend(perp_contracts[idx].events.FundingPaid.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))
        logs.extend(perp_contracts[idx].events.PerpetualParametersChanged.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block))

    logs = sorted(logs, key = lambda x: (x['blockNumber'], x['transactionIndex']))

    lp_update_list = []
    for log in logs:
        log_type = log['event']
        if log_type == 'ClearingHouseParametersChanged':
            handle_clearinghouse_parameters_changed(log)
        elif log_type == 'PerpetualParametersChanged':
            handle_perpetual_parameters_changed(log)
        elif log_type == 'CollateralAdded':
            handle_collateral_added(log)
        elif log_type == 'CollateralWeightChanged':
            handle_collateral_weight_changed(log)
        elif log_type == 'MarketRemoved':
            handle_market_removed(log)
        elif log_type == 'Deposit':
            handle_deposit(log)
        elif log_type == 'Withdraw':
            handle_withdraw(log)
        elif log_type == 'ChangePosition':
            handle_change_position(log)
        elif log_type == 'LiquidityProvided':
            handle_liquidity_added(log, lp_update_list)
        elif log_type == 'LiquidityRemoved':
            handle_liquidity_removed(log, lp_update_list)
        elif log_type == 'FundingPaid':
            handle_funding(log)
        elif log_type == 'LiquidationCall':
            handle_liquidation(log, lp_update_list)
        else:
            print('Something going very wrong, got unrecognized logs:')
            print(log)
            exit()

    for lp in lp_update_list:
        idx = lp[0]
        account = lp[1]
        lp_position = perp_contracts[idx].functions.getLpPosition(account).call()

        state['lp_positions'][idx][account]['open_notional'] = lp_position[0]
        state['lp_positions'][idx][account]['position_size'] = lp_position[1]
        state['lp_positions'][idx][account]['liquidity_balance'] = lp_position[2]
        state['lp_positions'][idx][account]['cumulative_funding_rate_per_lp_token'] = lp_position[7]
        state['lp_positions'][idx][account]['total_quote_fees_growth'] = lp_position[6]
        state['lp_positions'][idx][account]['total_base_fees_growth'] = lp_position[5]
        state['lp_positions'][idx][account]['total_trading_fees_growth'] = lp_position[4]

def handle_clearinghouse_parameters_changed(event_log):
    args = event_log['args']
    state['min_margin'] = args['newMinMargin']
    state['ua_debt_seizure_threshold'] = args['uaDebtSeizureThreshold']
    state['non_ua_coll_seizure_discount'] = args['nonUACollSeizureDiscount']
    state['liquidation_reward'] = args['newLiquidationReward']
    state['liquidation_reward_insurance_share'] = args['newLiquidationRewardInsuranceShare']

def handle_perpetual_parameters_changed(event_log):
    args = event_log['args']
    address = event_log['address']
    idx = addresses_to_idx[address]
    state['perps'][idx]['risk_weight'] = args['newRiskWeight']
    state['perps'][idx]['lp_debt_coef'] = args['newLpDebtCoef']

def handle_collateral_added(event_log):
    args = event_log['args']
    asset = args['asset']
    weight = args['weight']
    state['reserve_weights'][asset] = weight

def handle_collateral_weight_changed(event_log):
    args = event_log['args']
    asset = args['asset']
    weight = args['newWeight']
    state['reserve_weights'][asset] = weight

def handle_deposit(event_log):
    args = event_log['args']
    user = args['user']
    asset = args['asset']
    amount = args['amount']

    if user not in state['reserves']:
        state['reserves'][user] = {}

    if asset not in state['reserves'][user]:
        state['reserves'][user][asset] = 0

    state['reserves'][user][asset] += amount

def handle_withdraw(event_log):
    args = event_log['args']
    user = args['user']
    asset = args['asset']
    amount = args['amount']

    state['reserves'][user][asset] -= amount

def handle_market_removed(event_log):
    args = event_log['args']
    idx = str(args['delistedIdx'])
    del state['trader_positions'][idx]
    del state['perps'][idx]

def handle_funding(event_log):
    idx = addresses_to_idx[event_log['address']]
    args = event_log['args']
    account = args['account']
    amount = args['amount']
    is_trader = args['isTrader']
    new_cumulative_funding = args['globalCumulativeFundingRate']

    if account not in state['reserves']:
        state['reserves'][account] = {}

    if state['ua_address'] not in state['reserves'][account]:
        state['reserves'][account][state['ua_address']] = 0

    state['reserves'][account][state['ua_address']] += amount
    
    if is_trader and account in state['trader_positions'][idx]:
        state['trader_positions'][idx][account]['cumulative_funding_rate'] = new_cumulative_funding
    elif not is_trader and account in state['lp_positions'][idx]:
        state['lp_positions'][idx][account]['cumulative_funding_rate_per_lp_token'] = new_cumulative_funding

def handle_change_position(event_log):
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

def handle_liquidity_added(event_log, lp_update_list):
    args = event_log['args']
    idx = str(args['idx'])
    provider = args['liquidityProvider']
    fees_earned = args['tradingFeesEarned']
    state['reserves'][provider][state['ua_address']] += fees_earned

    if provider not in state['lp_positions'][idx]:
        state['lp_positions'][idx][provider] = {}

    if (idx, provider) not in lp_update_list:
        lp_update_list.append((idx, provider))

def handle_liquidity_removed(event_log, lp_update_list):
    args = event_log['args']
    idx = str(args['idx'])
    provider = args['liquidityProvider']
    profit = args['profit']
    is_closed = args['isPositionClosed']
    state['reserves'][provider][state['ua_address']] += profit
    if is_closed:
        del state['lp_positions'][idx][provider]
        if (idx, provider) in lp_update_list:
            lp_update_list.remove((idx, provider))
    elif (idx, provider) not in lp_update_list:
        lp_update_list.append((idx, provider))


def handle_liquidation(event_log, lp_update_list):
    args = event_log['args']
    idx = str(args['idx'])
    liquidatee = args['liquidatee']
    liquidator = args['liquidator']
    notional = args['notional']
    profit = args['profit']
    is_trader = args['isTrader']

    liquidation_reward_amount = int(notional / (10**18) * state['liquidation_reward'])
    insurance_liquidation_reward = int(liquidation_reward_amount / (10**18) * state['liquidation_reward_insurance_share'])
    liquidator_liquidation_reward = liquidation_reward_amount - insurance_liquidation_reward

    if liquidator not in state['reserves']:
        state['reserves'][liquidator] = {}

    if state['ua_address'] not in state['reserves'][liquidator]:
        state['reserves'][liquidator][state['ua_address']] = 0

    state['reserves'][liquidator][state['ua_address']] += liquidator_liquidation_reward
    state['reserves'][liquidatee][state['ua_address']] += profit

    if is_trader:
        del state['trader_positions'][idx][liquidatee]
    else:
        del state['lp_positions'][idx][liquidatee]
        if (idx, liquidatee) in lp_update_list:
            lp_update_list.remove((idx, liquidatee))

    if liquidator == account.address:
        state['liquidation_rewards'] += liquidator_liquidation_reward
        print(f'Detected liquidation with reward of {liquidator_liquidation_reward / (10**18)}')


def sync_markets_added(to_block):
    logs = clearinghouse_contract.events.MarketAdded.get_logs(fromBlock=state['synced_block']+1, toBlock=to_block)
    for event_log in logs:
        args = event_log['args']
        idx = str(args['listedIdx'])
        perp_address = args['perpetual']

        market_address = web3.eth.contract(address=perp_address, abi=perp_abi).functions.market().call()
        market_out_fee = web3.eth.contract(address=market_address, abi=market_abi).functions.out_fee().call()

        state['trader_positions'][idx] = {}
        state['lp_positions'][idx] = {}
        state['perps'][idx] = {
            'address': perp_address,
            'market_address': market_address,
            'market_out_fee': market_out_fee
        }
        

    for idx in state['perps']:
        addresses_to_idx[state['perps'][idx]['address']] = idx
        perp_contracts[idx] = web3.eth.contract(address=state['perps'][idx]['address'], abi=perp_abi)

def sync_perps():
    for idx in state['perps']:
        contract = perp_contracts[idx]
        global_position = contract.functions.getGlobalPosition().call()

        if idx not in state['global_positions']:
            state['global_positions'][idx] = {}

        state['global_positions'][idx]['cumulative_funding_rate'] = global_position[2]
        state['global_positions'][idx]['cumulative_funding_rate_per_lp_token'] = global_position[5]
        state['global_positions'][idx]['total_quote_fees_growth'] = global_position[9]
        state['global_positions'][idx]['total_base_fees_growth'] = global_position[8]
        state['global_positions'][idx]['total_trading_fees_growth'] = global_position[7]

        index_price = contract.functions.indexPrice().call()
        state['perps'][idx]['index_price'] = index_price

        total_liquidity_provided = contract.functions.getTotalLiquidityProvided().call()
        state['perps'][idx]['total_liquidity_provided'] = total_liquidity_provided

        market_contract = web3.eth.contract(address=state['perps'][idx]['market_address'], abi=market_abi)
        quote_balance = market_contract.functions.balances(VQUOTE_INDEX).call()
        base_balance = market_contract.functions.balances(VBASE_INDEX).call()
        state['perps'][idx]['base_balance'] = base_balance
        state['perps'][idx]['quote_balance'] = quote_balance



### HELPER FUNCTIONS ###

def get_pnl_across_markets(trader):
    trader_pnl = 0
    lp_pnl = 0
    for idx in state['perps']:
        oracle_price = state['perps'][idx]['index_price']

        if trader in state['trader_positions'][idx]:
            position_size = state['trader_positions'][idx][trader]['position_size']
            open_notional = state['trader_positions'][idx][trader]['open_notional']

            v_quote_virtual_proceeds = int(oracle_price / (10**18) * position_size)
            fees_in_wad = state['perps'][idx]['market_out_fee'] * 10**(18 - CURVE_TRADING_FEE_DECIMALS)
            trading_fees = int(abs(v_quote_virtual_proceeds) / (10**18) * fees_in_wad)

            trader_pnl += open_notional + v_quote_virtual_proceeds - trading_fees

        if trader in state['lp_positions'][idx]:
            open_notional, position_size = get_lp_position_after_withdrawal(trader, idx)

            v_quote_virtual_proceeds = int(oracle_price / (10**18) * position_size)
            fees_in_wad = state['perps'][idx]['market_out_fee'] * 10**(18 - CURVE_TRADING_FEE_DECIMALS)
            trading_fees = int(abs(v_quote_virtual_proceeds) / (10**18) * fees_in_wad)

            unrealized_lp_pnl = open_notional + v_quote_virtual_proceeds - trading_fees
            lp_pnl += unrealized_lp_pnl + get_lp_trading_fees(trader, idx)
            
    return trader_pnl + lp_pnl


def get_debt_across_markets(trader):
    trader_debt = 0
    lp_debt = 0
    for idx in state['perps']:
        oracle_price = state['perps'][idx]['index_price']
        risk_weight = state['perps'][idx]['risk_weight']

        if trader in state['trader_positions'][idx]:
            position_size = state['trader_positions'][idx][trader]['position_size']
            open_notional = state['trader_positions'][idx][trader]['open_notional']

            quote_debt = min(open_notional, 0)
            base_debt = min(int(position_size / (10**18) * oracle_price), 0)

            market_trader_debt = abs(quote_debt + base_debt)
            trader_debt += int(market_trader_debt / (10**18) * risk_weight)

        if trader in state['lp_positions'][idx]:
            position_size = state['lp_positions'][idx][trader]['position_size']
            open_notional = state['lp_positions'][idx][trader]['open_notional']

            lp_debt_coef = state['perps'][idx]['lp_debt_coef']

            quote_debt = min(open_notional, 0)
            base_debt = min(int(position_size / (10**18) * oracle_price), 0)

            market_lp_debt = int(abs(quote_debt + base_debt) / (10**18) * lp_debt_coef)
            lp_debt += int(market_lp_debt / (10**18) * risk_weight)

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

def is_position_valid(address):
    min_margin = state['min_margin']

    pnl = get_pnl_across_markets(address)
    pending_funding_payments = get_pending_funding_payments(address)
    reserve_value = get_reserve_value(address)
    # note this is different than the actual contract to factor pending funding payments
    total_collateral_value = reserve_value + pending_funding_payments
    margin_required = get_total_margin_requirement(address, min_margin)

    free_collateral = min(total_collateral_value, total_collateral_value + pnl) - margin_required
    return free_collateral >= -0.05 * (10**18)

def get_pending_funding_payments(address):
    trader_funding = 0
    lp_funding = 0

    for idx in state['perps']:
        if address in state['trader_positions'][idx]:
            position_size = state['trader_positions'][idx][address]['position_size']
            if 'cumulative_funding_rate' not in state['trader_positions'][idx][address]:
                state['trader_positions'][idx][address]['cumulative_funding_rate'] = perp_contracts[idx].functions.getTraderPosition(address).call()[2]
            user_cumulative_funding_rate = state['trader_positions'][idx][address]['cumulative_funding_rate']
            global_cumulative_funding_rate = state['global_positions'][idx]['cumulative_funding_rate']

            if position_size > 0:
                funding_rate = user_cumulative_funding_rate - global_cumulative_funding_rate
            else:
                funding_rate = global_cumulative_funding_rate - user_cumulative_funding_rate
            trader_funding += int(funding_rate / (10**18) * abs(position_size))

        if address in state['lp_positions'][idx]:
            liquidity_balance = state['lp_positions'][idx][address]['liquidity_balance']
            user_cumulative_funding_rate_per_lp_token = state['lp_positions'][idx][address]['cumulative_funding_rate_per_lp_token']
            global_cumulative_funding_rate_per_lp_token = state['global_positions'][idx]['cumulative_funding_rate_per_lp_token']

            lp_funding += int((global_cumulative_funding_rate_per_lp_token - user_cumulative_funding_rate_per_lp_token) / (10**18) * liquidity_balance)

    return trader_funding + lp_funding


def get_lp_position_after_withdrawal(lp_address, idx):
    lp_open_notional = state['lp_positions'][idx][lp_address]['open_notional']
    lp_position_size = state['lp_positions'][idx][lp_address]['position_size']
    lp_liquidity_balance = state['lp_positions'][idx][lp_address]['liquidity_balance']
    lp_total_quote_fees_growth = state['lp_positions'][idx][lp_address]['total_quote_fees_growth']
    lp_total_base_fees_growth = state['lp_positions'][idx][lp_address]['total_base_fees_growth']
    
    global_total_quote_fees_growth = state['global_positions'][idx]['total_quote_fees_growth']
    global_total_base_fees_growth = state['global_positions'][idx]['total_base_fees_growth']

    market_quote_balance = state['perps'][idx]['quote_balance']
    market_base_balance = state['perps'][idx]['base_balance']

    total_liquidity_provided = state['perps'][idx]['total_liquidity_provided']

    quote_tokens_ex_fees, _ = get_virtual_tokens_withdrawn_from_curve_pool(
            total_liquidity_provided,
            lp_liquidity_balance,
            market_quote_balance,
            lp_total_quote_fees_growth,
            global_total_quote_fees_growth
        )

    base_tokens_ex_fees, _ = get_virtual_tokens_withdrawn_from_curve_pool(
            total_liquidity_provided,
            lp_liquidity_balance,
            market_base_balance,
            lp_total_base_fees_growth,
            global_total_base_fees_growth
        )

    open_notional = lp_open_notional + quote_tokens_ex_fees
    position_size = lp_position_size + base_tokens_ex_fees

    return (open_notional, position_size)

def get_virtual_tokens_withdrawn_from_curve_pool(
        total_liquidity_provided, 
        lp_tokens_liquidity_provider, 
        curve_pool_balance,
        user_virtual_token_growth_rate,
        global_virtual_token_total_growth
    ):
    if total_liquidity_provided == 0:
        return (0, 0)

    tokens_incl_fees = ((lp_tokens_liquidity_provider - 1) * curve_pool_balance) / total_liquidity_provided
    tokens_ex_fees = int(tokens_incl_fees / (10**18 + global_virtual_token_total_growth - user_virtual_token_growth_rate) * (10**18))

    return (tokens_ex_fees, tokens_incl_fees)

def get_lp_trading_fees(lp_address, idx):
    liquidity_balance = state['lp_positions'][idx][lp_address]['liquidity_balance']
    fee_growth_difference = state['global_positions'][idx]['total_trading_fees_growth'] - state['lp_positions'][idx][lp_address]['total_trading_fees_growth']
    return int(liquidity_balance / (10**18) * fee_growth_difference)

# Submits transaction
def liquidate_position(address, idx, is_trader):
    proposed_amount = None
    try:
        if is_trader:
            proposed_amount = clearinghouse_viewer_contract.functions.getTraderProposedAmount(idx, address, int(1e18), 100, 0).call()
        else:
            proposed_amount = clearinghouse_viewer_contract.functions.getLpProposedAmount(idx, address, int(1e18), 100, [0,0], 0).call()
    except Exception as e:
        print(f'Fail: Position: {clearinghouse_viewer_contract.functions.getTraderPosition(idx, address).call()}')

    if proposed_amount is not None:

        if is_trader:
            unsigned_tx = clearinghouse_contract.functions.liquidateTrader(idx, address, proposed_amount, 0).build_transaction(transaction_dict)
        else:
            unsigned_tx = clearinghouse_contract.functions.liquidateLp(idx, address, [0,0], proposed_amount, 0).build_transaction(transaction_dict)

        signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
        try:
            tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
            transaction_dict['nonce'] += 1
            return receipt['status']
        except ValueError:
            transaction_dict['nonce'] = web3.eth.get_transaction_count(account.address)
            print('Attempted liquidation but nonce was incorrect. Will retry soon.\n')

    return 0

def main():
    last_heartbeat = 0
    while True:
        last_block = web3.eth.block_number
        sync(last_block)
        for idx in state['perps']:

            # Check trader positions
            for trader in state['trader_positions'][idx]:
                if not is_position_valid(trader):
                    status = liquidate_position(trader, int(idx), True)
                    print(f'Liquidated trader on market {idx}: {trader}. Liquidation status: {status}.')
                    if status == 0: # Safety measure to avoid rapid firing failed transactions
                        time.sleep(60)

            # Check LP positions
            for lp in state['lp_positions'][idx]:
                if not is_position_valid(lp):
                    status = liquidate_position(lp, int(idx), False)
                    print(f'Liquidated LP on market {idx}: {lp}. Liquidation status: {status}.')
                    if status == 0: # Safety measure to avoid rapid firing failed transactions
                        time.sleep(60)

        # Heartbeat
        if time.time() - last_heartbeat > 90:
            last_heartbeat = time.time()
            print(f'Heartbeat at block: {last_block}')
            print(f"Total earned liquidation rewards: {round(state['liquidation_rewards'] / (10**18), 2)}")
            print()

        # No point syncing and checking again until the block changes
        while web3.eth.block_number == last_block:
            time.sleep(0.1)


if __name__ == '__main__':
    while True:
        try:
            web3 = Web3(Web3.WebsocketProvider(rpc_url))
            main()
        except Exception as e:
           print(f'Exception occured: {e}\n')
           time.sleep(60)
