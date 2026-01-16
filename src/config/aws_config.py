"""
AWS Configuration Module
Handles AWS Secrets Manager integration and cloud services configuration
"""

import json
from typing import Dict, Any, Optional
import boto3
from botocore.exceptions import ClientError

from src.config.constants import AWS_REGION, AWS_SECRET_ID
from src.utils.logger import get_logger
from src.utils.exceptions import ConfigurationError


logger = get_logger(__name__)


class AWSConfig:
    """
    Manages AWS service configurations and secret retrieval
    Implements singleton pattern for efficient resource usage
    """

    _instance: Optional['AWSConfig'] = None
    _secrets_cache: Optional[Dict[str, Any]] = None

    def __new__(cls):
        """Singleton pattern implementation"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize AWS clients"""
        if not hasattr(self, '_initialized'):
            self.region = AWS_REGION
            self.secret_id = AWS_SECRET_ID
            self._secrets_client = None
            self._initialized = True
            logger.info(f"AWS Config initialized for region: {self.region}")

    @property
    def secrets_client(self):
        """Lazy initialization of Secrets Manager client"""
        if self._secrets_client is None:
            try:
                self._secrets_client = boto3.client(
                    'secretsmanager',
                    region_name=self.region
                )
                logger.debug("AWS Secrets Manager client initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Secrets Manager client: {e}")
                raise ConfigurationError(
                    f"AWS Secrets Manager client initialization failed: {e}"
                )
        return self._secrets_client

    def get_secrets(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Retrieve secrets from AWS Secrets Manager with caching
        
        Args:
            force_refresh: Force refresh cached secrets
            
        Returns:
            Dictionary containing secret key-value pairs
            
        Raises:
            ConfigurationError: If secrets cannot be retrieved
        """
        # Return cached secrets if available and not forcing refresh
        if self._secrets_cache is not None and not force_refresh:
            logger.debug("Returning cached secrets")
            return self._secrets_cache

        try:
            logger.info(f"Retrieving secrets from AWS Secrets Manager: {self.secret_id}")
            
            response = self.secrets_client.get_secret_value(SecretId=self.secret_id)
            
            # Parse secret string (assuming JSON format)
            if 'SecretString' in response:
                secrets = json.loads(response['SecretString'])
            else:
                # Binary secrets not expected for this use case
                raise ConfigurationError("Binary secrets not supported")

            # Validate required secret keys
            self._validate_secrets(secrets)
            
            # Cache secrets for reuse
            self._secrets_cache = secrets
            logger.info("Secrets successfully retrieved and cached")
            
            return secrets

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            
            logger.error(
                f"AWS Secrets Manager error: {error_code} - {error_message}"
            )
            
            # Provide helpful error messages for common issues
            if error_code == 'ResourceNotFoundException':
                raise ConfigurationError(
                    f"Secret '{self.secret_id}' not found in region '{self.region}'"
                )
            elif error_code == 'AccessDeniedException':
                raise ConfigurationError(
                    f"Access denied to secret '{self.secret_id}'. "
                    "Check IAM permissions."
                )
            elif error_code == 'InvalidRequestException':
                raise ConfigurationError(
                    f"Invalid request for secret '{self.secret_id}': {error_message}"
                )
            else:
                raise ConfigurationError(
                    f"Failed to retrieve secrets: {error_code} - {error_message}"
                )
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse secret JSON: {e}")
            raise ConfigurationError(
                f"Secret value is not valid JSON: {e}"
            )
            
        except Exception as e:
            logger.error(f"Unexpected error retrieving secrets: {e}")
            raise ConfigurationError(
                f"Unexpected error retrieving secrets: {e}"
            )

    def _validate_secrets(self, secrets: Dict[str, Any]) -> None:
        """
        Validate that all required secret keys are present
        
        Args:
            secrets: Dictionary of secrets to validate
            
        Raises:
            ConfigurationError: If required keys are missing
        """
        required_keys = ['WALLET_PRIVATE_KEY', 'POLY_API_KEY', 'POLY_API_SECRET', 'POLY_API_PASS']
        
        missing_keys = [key for key in required_keys if key not in secrets]
        
        if missing_keys:
            raise ConfigurationError(
                f"Missing required secret keys: {', '.join(missing_keys)}"
            )
        
        logger.debug("All required secret keys validated")

    def get_wallet_private_key(self) -> str:
        """
        Convenience method to get wallet private key
        
        Returns:
            Wallet private key string
        """
        secrets = self.get_secrets()
        return secrets['WALLET_PRIVATE_KEY']
    
    def get_api_credentials(self) -> Dict[str, str]:
        """
        Retrieve Level 2 (L2) API credentials from AWS Secrets Manager.
        
        L2 authentication is required for:
        - post_order() - Placing BUY/SELL orders
        - cancel_order() - Canceling active orders  
        - cancel_all() - Bulk order cancellation
        - get_orders() - Fetching order history
        - get_trades() - Fetching trade history
        
        L1 authentication (private key only) works for:
        - Market data (prices, order books)
        - Position queries
        - Balance checks
        
        Returns:
            Dict with api_key, api_secret, api_passphrase
            
        Raises:
            ConfigurationError: If any credential key is missing from Secrets Manager
            
        Security Notes:
            - Credentials are wallet-specific (one set per wallet)
            - Creating new credentials invalidates previous ones
            - Store securely in AWS Secrets Manager only
        """
        secrets = self.get_secrets()
        
        return {
            'api_key': secrets['POLY_API_KEY'],
            'api_secret': secrets['POLY_API_SECRET'],
            'api_passphrase': secrets['POLY_API_PASS']
        }
    
    def update_api_credentials(self, api_key: str, api_secret: str, api_passphrase: str) -> None:
        """
        Update L2 API credentials in AWS Secrets Manager.
        
        Use Cases:
            - Rotating credentials for security
            - Recovering from credential invalidation
            - Updating after manual credential regeneration
        
        WARNING: Creating new Polymarket API credentials invalidates old ones.
                 Only update here after generating new credentials via Polymarket.
        
        Args:
            api_key: CLOB API key (UUID format)
            api_secret: CLOB API secret (64-char hex string)
            api_passphrase: CLOB API passphrase (64-char hex string)
            
        Raises:
            ConfigurationError: If AWS Secrets Manager update fails
            
        Security:
            - Requires IAM role with secretsmanager:UpdateSecret permission
            - Clears cache to force immediate reload
            - Atomic update (all 3 credentials updated together)
        """
        try:
            # Get existing secrets
            secrets = self.get_secrets()
            
            # Update with new API credentials
            secrets['POLY_API_KEY'] = api_key
            secrets['POLY_API_SECRET'] = api_secret
            secrets['POLY_API_PASS'] = api_passphrase
            
            # Store updated secrets
            self.secrets_client.update_secret(
                SecretId=self.secret_id,
                SecretString=json.dumps(secrets)
            )
            
            # Clear cache to force refresh on next access
            self.clear_cache()
            
            logger.info("L2 API credentials updated in Secrets Manager")
            
        except Exception as e:
            logger.error(f"Failed to update API credentials: {e}")
            raise ConfigurationError(f"Failed to update API credentials: {e}")

    def clear_cache(self) -> None:
        """Clear cached secrets (useful for testing or forced refresh)"""
        self._secrets_cache = None
        logger.debug("Secrets cache cleared")


# Singleton instance
aws_config = AWSConfig()


def get_aws_config() -> AWSConfig:
    """
    Get the singleton AWS configuration instance
    
    Returns:
        AWSConfig singleton instance
    """
    return aws_config
