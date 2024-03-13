# Increment Liquidation Bot

## Prerequisites

You will need Python 3 installed before proceeding with the rest of setup.
You will also need a private key for a wallet with enough ETH for gas. Liquidations will stop if the account is allowed to run out of gas.

## Setup
Install required Python packages by running:
```
pip install -r requirements.txt
```

Alternatively, the only external packages used can be installed by running:
```
pip install web3
pip install python-dotenv
```

Prepare a .env file with the following variables:
```
# URL of websocket supported RPC node, preferably localhost
RPC = wss://example.rpc.address

# Private key to make transactions from
PRIVATE_KEY = 0x....

# Name of the network to use, either zksync or zktestnet to specify mainnet vs testnet
NETWORK = zktestnet
```

## Running

Open a terminal window in the project root directory. The bot can be launched with:

`python3 Liquidation.py`
