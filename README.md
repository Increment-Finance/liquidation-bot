# Increment Liquidation Bot

## Prerequisites

You will need Python 3 installed before proceeding with the rest of setup.
You will also need GETH to create the required keyfile.

## Setup

Install required Python packages by running:

```
pip install -r requirements.txt
```

Prepare a .env file with the following variables:

```
# URL of websocket supported RPC node, preferably localhost
RPC = wss://example.rpc.address

# Private key to make transactions from
PRIVATE_KEY = 0x....

# Url of the subgraph
SUBGRAPH_URL = https://api.thegraph.com/subgraphs/name/increment-finance/subgraph
```

## Running

Open a terminal window in the project root directory. The bot can be launched with:

`python3 Liquidation.py`
