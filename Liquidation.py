import json
import requests
import time
import datetime
import os
import getpass
from dataclasses import dataclass

from web3 import Web3, Account
from web3.middleware import geth_poa_middleware
from dotenv import load_dotenv, dotenv_values


load_dotenv('.env')

# This RPC should ideally be localhost
rpc_url = os.getenv('RPC')
web3 = Web3(Web3.WebsocketProvider(rpc_url, websocket_timeout=60))

password = os.getenv('PASSWORD')

graph_url = 'https://api.thegraph.com/subgraphs/name/increment-finance/beta-v2-zksync-testnet'

# Required to make compatible with Rinkeby testnet
if web3.eth.chainId == 4:
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)


# Load in contracts we will interact with
with open(f'./contract-details/{web3.eth.chainId}/ClearingHouse.json', 'r') as clearinghouse_json:
    clearinghouse = json.load(clearinghouse_json)
clearinghouse_contract = web3.eth.contract(address=clearinghouse['address'], abi=clearinghouse['abi'])

with open(f'./contract-details/{web3.eth.chainId}/ClearingHouseViewer.json', 'r') as clearinghouse_viewer_json:
    clearinghouse_viewer = json.load(clearinghouse_viewer_json)
clearinghouse_viewer_contract = web3.eth.contract(address=clearinghouse_viewer['address'], abi=clearinghouse_viewer['abi'])

with open(f'./contract-details/{web3.eth.chainId}/Vault.json', 'r') as vault_json:
    vault = json.load(vault_json)
vault_contract = web3.eth.contract(address=vault['address'], abi=vault['abi'])

# Setup wallet from keyfile
with open(f'./{os.getenv("KEYFILE")}') as keyfile:
    account = Account.from_key(web3.eth.account.decrypt(keyfile.read(), password))#getpass.getpass()))
    print(f'Password accepted, using account {account.address}')

transaction_dict = {
    'chainId': web3.eth.chainId,
    'nonce': web3.eth.get_transaction_count(account.address),
    'gasPrice': web3.toWei(0.1, 'gwei'),
    'gas': 3*(10**6)
}


@dataclass
class UserPosition:
    idx: int
    user: str
    is_trader: bool

@dataclass
class UserDebtPosition:
    user: str
    ua_balance: int


def Initialize_User_Positions(idx):
    positions_returned = 1000
    query_iter = 0
    position_list = []

    while positions_returned == 1000:

        ### Query used without formatting for python:
        # {
        #   market(id: x) {
        #     positions(first: 1000, skip: y) {
        #       user {
        #         id
        #       }
        #       amount
        #     }
        #   }
        # }

        graph_query = ( '{\n'
                            f'market(id: {idx}) {{\n'
                                f'positions(first: 1000, skip: {query_iter * 1000}) {{\n'
                                    'user {\n'
                                        'id\n'
                                    '}\n'
                                    'positionSize\n'
                                '}\n'
                                f'liquidityPositions(first: 1000, skip: {query_iter * 1000}) {{\n'
                                    'user {\n'
                                        'id\n'
                                    '}\n'
                                    'positionSize\n'
                                '}\n'
                            '}\n'
                        '}')

        request = requests.post(graph_url, json={'query': graph_query})

        trader_results = request.json()['data']['market']['positions']
        lp_results = request.json()['data']['market']['liquidityPositions']
        positions_returned = max(len(trader_results), len(lp_results))

        query_iter += 1

        for trader_position in trader_results:
            position_size = int(trader_position['positionSize'])
            user_address = Web3.toChecksumAddress(trader_position['user']['id'])
            if position_size != 0:
                position = UserPosition(idx=idx, user=user_address, is_trader=True)
                position_list.append(position)

        for lp_position in lp_results:
            position_size = int(lp_position['positionSize'])
            user_address = Web3.toChecksumAddress(lp_position['user']['id'])
            if position_size != 0:
                position = UserPosition(idx=idx, user=user_address, is_trader=False)
                position_list.append(position)

    return position_list


def Initialize_User_Debt_Positions():
    positions_returned = 1000
    query_iter = 0
    position_list = []

    while positions_returned == 1000:

        ### Query used without formatting for python:
        # {
        #   currentTokenBalances(first: 1000, skip: 0) {
        #     user {
        #       id
        #     }
        #     amount
        #     token {
        #       id
        #     }
        #   }
        # }

        graph_query = ( '{\n'
                            f'currentTokenBalances(first: 1000, skip: {query_iter * 1000}) {{\n'
                                'user {\n'
                                    'id\n'
                                '}\n'
                                'amount\n'
                                'token {\n'
                                    'id\n'
                                '}\n'
                            '}\n'
                        '}\n')


        request = requests.post(graph_url, json={'query': graph_query})

        token_balances = request.json()['data']['currentTokenBalances']
        positions_returned = len(token_balances)
        query_iter += 1

        for token_balance in token_balances:
            if int(token_balance['token']['id']) == 0:# and int(token_balance['amount']) < 0:
                user_address = Web3.toChecksumAddress(token_balance['user']['id'])
                ua_balance = int(token_balance['amount'])
                debt_position = UserDebtPosition(user=user_address, ua_balance=ua_balance)
                position_list.append(debt_position)

    return position_list


# Submits transaction
def Liquidate_Position(position):
    idx = position.idx
    address = position.user
    is_trader = position.is_trader

    if is_trader:
        proposed_amount = clearinghouse_viewer_contract.functions.getTraderProposedAmount(idx, address, int(1e18), 100, 0).call()
        unsigned_tx = clearinghouse_contract.functions.liquidateTrader(idx, address, proposed_amount, 0).buildTransaction(transaction_dict)
    else:
        proposed_amount = clearinghouse_viewer_contract.functions.getLpProposedAmount(idx, address, int(1e18), 100, [0,0]).call()
        unsigned_tx = clearinghouse_contract.functions.liquidateLp(idx, address, [0,0], proposed_amount, 0).buildTransaction(transaction_dict)
                             
    signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    transaction_dict['nonce'] += 1

    return receipt


def Seize_Collateral(debt_position):
    address = debt_position.user

    unsigned_tx = clearinghouse_contract.functions.seizeCollateral(address)

    signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    transaction_dict['nonce'] += 1

    return receipt


def main():
    ## Initialization
    min_margin = clearinghouse_contract.functions.minMargin().call()
    ua_debt_threshold = clearinghouse_contract.functions.uaDebtSeizureThreshold().call()
    non_UA_coll_seizure_discount = clearinghouse_contract.functions.nonUACollSeizureDiscount().call()

    heartbeat = 0
    position_list = []


    ## Main loop
    while True:
        if heartbeat % 60 == 0:
            num_perpetual_markets = clearinghouse_contract.functions.getNumMarkets().call()

            position_list = []
            for idx in range(num_perpetual_markets):
                position_list.extend(Initialize_User_Positions(idx))

            debt_position_list = Initialize_User_Debt_Positions()

            print(f'Currently tracking {len(position_list)} open position(s) and {len(debt_position_list)} UA debt position(s).\n')

        # Check if any open positions can be liquidated
        for position in position_list:
            # TODO: Multicall should be used here to group all the marginIsValid() calls, will save seconds if lots of positions are open
            if not clearinghouse_contract.functions.getFreeCollateralByRatio(position.user, min_margin).call() >= 0:
                print(f'Liquidating user {position.user} on idx {position.idx}.')
                receipt = Liquidate_Position(position)
                print('Success\n' if receipt.status else 'Fail\n')

        for debt_position in debt_position_list:
            debt = -debt_position.ua_balance
            discounted_collaterals_balance = vault_contract.functions.getReserveValue(debt_position.user, True).call()
            discounted_collaterals_balance_ex_UA = discounted_collaterals_balance - debt_position.ua_balance

            if debt > ua_debt_threshold or debt > (discounted_collaterals_balance_ex_UA * non_UA_coll_seizure_discount) // 10**18:
                print(f'Seizing collateral of user {debt_position.user}.')
                receipt = Seize_Collateral(debt_position)
                print('Success\n' if receipt.status else 'Fail\n')

        # Ideally this sleep timer is replaced by a block header filter, assumes user has a websocket RPC available
        time.sleep(20)
        heartbeat += 1


if __name__ == '__main__':
   main()