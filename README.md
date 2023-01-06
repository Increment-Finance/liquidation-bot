# Increment Arbitrage Bot
## Prerequisites
You will need Python 3 installed before proceeding with the rest of setup.
You will also need GETH to create the required keyfile.

## Setup
Install submodules

```
git submodule update --init
```

Install required Python packages by running:

```
pip install -r requirements.txt
```

Prepare a .env file with the following variables:

```
# URL of websocket supported RPC node, preferably localhost
RPC = wss://example.rpc.address

# File name of GETH keyfile to make transactions from
KEYFILE = keyfilename

# Password to decrypt keyfile
PASSWORD = keyfilepassword

# Url of the subgraph
SUBGRAPH_URL = https://api.thegraph.com/subgraphs/name/increment-finance/subgraph
```
Use GETH to create a keyfile to be used to submit transactions and place it in the root directory of the project. Be sure the file name matches the name set in .env.

## Running
Open a terminal window in the project root directory. The bot can be launched with:

`python3 Liquidation.py`

You will be prompted for the password to decrypt your keyfile to submit transactions.
If the password is correct, the bot will immediately start monitoring all active perpetual positions and liquidating when appropriate.
