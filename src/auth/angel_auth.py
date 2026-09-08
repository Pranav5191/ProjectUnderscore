import os
import pyotp
from SmartApi.smartConnect import SmartConnect
from logzero import logger

class AngelAuthenticator:
    def __init__(self):
        self.api_key = os.getenv("ANGEL_API_KEY")
        self.client_code = os.getenv("ANGEL_CLIENT_ID")
        self.pin = os.getenv("ANGEL_PIN")
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET")
        self.smart_connect = SmartConnect(api_key=self.api_key)

    def generate_session(self):
        try:
            totp_code = pyotp.TOTP(self.totp_secret).now()
            data = self.smart_connect.generateSession(self.client_code, self.pin, totp_code)
            
            if data and data.get("status"):
                auth_data = data["data"]
                jwt_token = auth_data["jwtToken"] 
                feed_token = self.smart_connect.getfeedToken()
                
                return {
                    "jwt_token": jwt_token,
                    "feed_token": feed_token,
                    "refresh_token": auth_data.get("refreshToken"),
                    "smart_connect_instance": self.smart_connect
                }
                
            logger.error(f"Authentication rejected: {data}")
            raise RuntimeError(f"Authentication rejected: {data}")
            
        except Exception as e:
            logger.error(f"Failed to generate Angel One session: {str(e)}")
            raise RuntimeError(f"Failed to generate Angel One session: {str(e)}")
