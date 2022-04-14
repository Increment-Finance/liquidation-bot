import json
import time
import datetime
import os
import getpass

from web3 import Web3, Account
from web3.middleware import geth_poa_middleware
from dotenv import load_dotenv, dotenv_values

from UserPosition import UserPosition


load_dotenv('.env')

# This RPC should ideally be localhost
rpc_url = os.getenv('RPC')
web3 = Web3(Web3.WebsocketProvider(rpc_url, websocket_timeout=60))

if web3.eth.chainId == 4:
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

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
    'maxFeePerGas': 3000000000,
    'maxPriorityFeePerGas': 2000000000
}


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

    # Create event filters
    extend_position_filter = clearinghouse_contract.events.ExtendPosition.createFilter(fromBlock=0)
    reduce_position_filter = clearinghouse_contract.events.ReducePosition.createFilter(fromBlock=0)

    extend_positions_init = extend_position_filter.get_all_entries()
    reduce_positions_init = reduce_position_filter.get_all_entries()

    position_list = []

    # Initialize from past 'ExtendPosition' events
    for position_extended in extend_positions_init:
        args = position_extended['args']
        position = UserPosition(args['idx'], args['user'])
        if position not in position_list:
            position_list.append(position)
        position_list[position_list.index(position)].AddToPosition(args['addedPositionSize'])

    # Initialize from past 'ReducePosition' events
    for position_reduced in reduce_positions_init:
        args = position_reduced['args']
        position = UserPosition(args['idx'], args['user'])
        position_list[position_list.index(position)].AddToPosition(args['reducedPositionSize'])

    # Get rid of closed positions (don't really have to do this, but stops position list from growing endlessly)
    position_list = [position for position in position_list if position.position_size != 0]

    print(f'Initialized, currently tracking {len(position_list)} open position(s).\n')

    ## Main loop
    while True:

        # Manage filters to track open positions
        extend_positions = extend_position_filter.get_new_entries()
        reduce_positions = reduce_position_filter.get_new_entries()

        for position_extended in extend_positions:
            args = position_extended['args']
            position = UserPosition(args['idx'], args['user'])
            if position not in position_list:
                position_list.append(position)
            position_list[position_list.index(position)].AddToPosition(args['addedPositionSize'])
 
        for position_reduced in reduce_positions:
            args = position_reduced['args']
            position = UserPosition(args['idx'], args['user'])
            position_list[position_list.index(position)].AddToPosition(args['reducedPositionSize'])

        # Remove closed positions from position list
        position_list = [position for position in position_list if position.position_size != 0]
        #print(f'Tracking {len(position_list)} open position(s).')

        # Check if any open positions can be liquidated
        for position in position_list:

            # TODO: Multicall should be used here to group all the marginIsValid() calls, will save seconds if lots of positions are open
            if not clearinghouse_contract.functions.marginIsValid(position.idx, position.user, MIN_MARGIN).call():
                print(f'Liquidating user {position.user} on idx {position.idx}.\n')

                receipt = liquidate_position(idx=position.idx, address=position.user)
                print(receipt)
                print('\n')

        # Ideally this sleep timer is replaced by a block header filter, assumes user has a websocket RPC available
        time.sleep(1)


if __name__ == '__main__':
   main()