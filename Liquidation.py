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


# Setup wallet from keyfile
with open(f'./{os.getenv("KEYFILE")}') as keyfile:
    account = Account.from_key(web3.eth.account.decrypt(keyfile.read(), getpass.getpass()))
    print(f'Password accepted, using account {account.address}')

transaction_dict = {
    'chainId': web3.eth.chainId,
    'nonce': web3.eth.get_transaction_count(account.address),
    'gas': 3*(10**6),
    'maxFeePerGas': web3.toWei(100, 'gwei'),
    'maxPriorityFeePerGas': web3.toWei(5, 'gwei')
}


@dataclass
class UserPosition:
    idx: int
    user: str


def Initialize_User_Positions(idx):
    graph_url = 'https://api.thegraph.com/subgraphs/name/increment-finance/increment-rinkeby'
    positions_returned = 1000
    query_num = 0
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
                                f'positions(first: 1000, skip: {query_num * 1000}) {{\n'
                                    'user {\n'
                                        'id\n'
                                    '}\n'
                                    'amount\n'
                                '}\n'
                            '}\n'
                        '}')

        request = requests.post(graph_url, json={'query': graph_query})
        results = request.json()['data']['market']['positions']
        positions_returned = len(results)
        query_num += 1

        for result_position in results:
            position_size = int(result_position['amount'])
            user_address = Web3.toChecksumAddress(result_position['user']['id'])

            if position_size != 0:
                position = UserPosition(idx=idx, user=user_address)
                position_list.append(position)

    return position_list


# Submits extendPosition transaction
def liquidate_position(idx, address):
    proposed_amount = clearinghouse_viewer_contract.functions.getProposedAmount(idx, address, 100).call()[0]

    unsigned_tx = clearinghouse_contract.functions.liquidate(idx, address, proposed_amount).buildTransaction(transaction_dict)
                                 
    signed_tx = web3.eth.account.sign_transaction(unsigned_tx, account.key)
    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

    transaction_dict['nonce'] += 1

    return receipt


def main():
    ## Initialization
    MIN_MARGIN = clearinghouse_contract.functions.MIN_MARGIN().call()
    market_added_filter = clearinghouse_contract.events.MarketAdded.createFilter(fromBlock=0)

    heartbeat = 0
    position_list = []


    ## Main loop
    while True:
        if heartbeat % 60 == 0:
            try:
                num_perpetual_markets = len(market_added_filter.get_all_entries())
            except ValueError:
                market_added_filter = clearinghouse_contract.events.MarketAdded.createFilter(fromBlock=0)
                num_perpetual_markets = len(market_added_filter.get_all_entries())

            position_list = []
            for idx in range(num_perpetual_markets):
                position_list.extend(Initialize_User_Positions(idx))
            print(f'Currently tracking {len(position_list)} open position(s).\n')

        # Check if any open positions can be liquidated
        for position in position_list:
            # TODO: Multicall should be used here to group all the marginIsValid() calls, will save seconds if lots of positions are open
            if not clearinghouse_contract.functions.marginIsValid(position.idx, position.user, MIN_MARGIN).call():
                print(f'Liquidating user {position.user} on idx {position.idx}.')
                receipt = liquidate_position(idx=position.idx, address=position.user)
                print('Success\n' if receipt.status else 'Fail\n')

        # Ideally this sleep timer is replaced by a block header filter, assumes user has a websocket RPC available
        time.sleep(1)
        heartbeat += 1


if __name__ == '__main__':
   main()