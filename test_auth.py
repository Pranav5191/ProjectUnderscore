import os
from dotenv import load_dotenv
from src.auth.angel_auth import AngelAuthenticator

load_dotenv()

try:
    auth = AngelAuthenticator()
    tokens = auth.generate_session()
    print("Authentication Successful!")
    print(f"JWT Token: {tokens['jwt_token'][:15]}...")
    print(f"Feed Token: {tokens['feed_token'][:15]}...")
except Exception as e:
    print(f"Authentication Failed: {e}")
