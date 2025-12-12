#!/usr/bin/env python3
"""
Test direct authentication with Air API using requests library
This uses the same flow as the successful curl command
"""

import os
import sys
from pathlib import Path
import requests
from dotenv import load_dotenv

# Load .env from .configs directory
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
ENV_FILE = REPO_DIR / ".configs" / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"Loaded .env from {ENV_FILE}")
else:
    print(f"Warning: .env not found at {ENV_FILE}")
    print("Please create .configs/.env with your credentials")

# Get credentials
api_token = os.getenv('AIR_API_TOKEN')
username = os.getenv('AIR_USERNAME', 'travisw@nvidia.com')
api_base_url = os.getenv('AIR_API_URL', 'https://air.nvidia.com')

# Remove trailing slashes and /api/vX
api_base_url = api_base_url.rstrip('/')
if api_base_url.endswith('/api/v2'):
    api_base_url = api_base_url[:-7]
if api_base_url.endswith('/api/v1'):
    api_base_url = api_base_url[:-7]

print(f"Testing direct API authentication...")
print(f"Base URL: {api_base_url}")
print(f"Username: {username}")
print(f"Token: {api_token[:10]}...{api_token[-4:]}\n")

# Step 1: Login to get JWT token
print("Step 1: Logging in to get JWT token...")
login_url = f"{api_base_url}/api/v1/login/"
print(f"Login URL: {login_url}")

try:
    response = requests.post(
        login_url,
        data={
            'username': username,
            'password': api_token
        }
    )
    
    print(f"Response status: {response.status_code}")
    print(f"Response headers: {dict(response.headers)}")
    print(f"Response body: {response.text[:500]}")
    
    if response.status_code == 200:
        result = response.json()
        if 'token' in result:
            jwt_token = result['token']
            print(f"\n✓ Login successful!")
            print(f"JWT Token: {jwt_token[:20]}...{jwt_token[-10:]}\n")
            
            # Step 2: Use JWT to access API
            print("Step 2: Testing API access with JWT token...")
            api_url = f"{api_base_url}/api/v2/simulations/"
            print(f"API URL: {api_url}")
            
            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Content-Type': 'application/json'
            }
            
            response2 = requests.get(api_url, headers=headers)
            print(f"Response status: {response2.status_code}")
            
            if response2.status_code == 200:
                data = response2.json()
                count = data.get('count', 0)
                print(f"\n✓ Successfully retrieved simulations!")
                print(f"Count: {count}")
                
                if 'results' in data and data['results']:
                    print(f"\nFirst simulation: {data['results'][0].get('title', 'N/A')}")
                
                print("\n" + "="*60)
                print("✓ Authentication flow working correctly!")
                print("="*60)
                print("\nYou can use this authentication method in the deployment script.")
            else:
                print(f"\n✗ API access failed: {response2.status_code}")
                print(f"Response: {response2.text[:500]}")
        else:
            print(f"\n✗ No token in response: {result}")
    else:
        print(f"\n✗ Login failed: {response.status_code}")
        print(f"Response: {response.text}")
        
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()

