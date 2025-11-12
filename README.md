# MetrANOVA SCinet Utilities

This is a repository to store utilities to help support the SCinet deployment at the annual supercomputing conference.

## Booth Metadata
This is a python script that relates IP prefixes to booth names. 

### Quickstart
```
cd booth-metadata
python -m venv ./venv
. ./venv/bin/activate
pip install -r requirements.txt
python booth_metadata.py --url https://scinet.supercomputing.org/nautobot --api-token YOUR_API_TOKEN_HERE
# prints result to screen
# add --output-file to output to file
```

### Quickstart (with docker)
```
cp env.example .env
# Add your API token to .env
docker compose up
#result will be in "./data" directory
```