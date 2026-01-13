"""
Tests for Configuration Module
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from botocore.exceptions import ClientError

from config.constants import *
from config.aws_config import AWSConfig, get_aws_config
from utils.exceptions import ConfigurationError


class TestConstants:
    """Test configuration constants"""
    
    def test_wallet_addresses(self):
        """Test wallet address constants are valid"""
        assert MIRROR_TARGET.startswith('0x')
        assert len(MIRROR_TARGET) == 42
        assert PROXY_WALLET_ADDRESS.startswith('0x')
        assert len(PROXY_WALLET_ADDRESS) == 42
    
    def test_trading_parameters(self):
        """Test trading parameter values"""
        assert 0 < ENTRY_PRICE_GUARD < 1
        assert DUST_THRESHOLD > 0
        assert 0 < MAX_SLIPPAGE_PERCENT < 1
        assert LOOP_INTERVAL_SEC > 0
    
    def test_aws_configuration(self):
        """Test AWS configuration constants"""
        assert AWS_REGION == "eu-central-1"
        assert AWS_SECRET_ID == "polymarket/prod/credentials"


class TestAWSConfig:
    """Test AWS configuration management"""
    
    def test_singleton_pattern(self):
        """Test AWSConfig is a singleton"""
        config1 = AWSConfig()
        config2 = AWSConfig()
        assert config1 is config2
    
    @patch('boto3.client')
    def test_secrets_client_initialization(self, mock_boto_client):
        """Test Secrets Manager client initialization"""
        config = AWSConfig()
        config._secrets_client = None  # Reset
        
        _ = config.secrets_client
        
        mock_boto_client.assert_called_once_with(
            'secretsmanager',
            region_name=AWS_REGION
        )
    
    @patch('boto3.client')
    def test_get_secrets_success(self, mock_boto_client):
        """Test successful secret retrieval"""
        mock_client = Mock()
        mock_client.get_secret_value.return_value = {
            'SecretString': '{"WALLET_PRIVATE_KEY": "0x123"}'
        }
        mock_boto_client.return_value = mock_client
        
        config = AWSConfig()
        config._secrets_cache = None  # Clear cache
        config._secrets_client = None  # Reset client
        
        secrets = config.get_secrets()
        
        assert 'WALLET_PRIVATE_KEY' in secrets
        assert secrets['WALLET_PRIVATE_KEY'] == '0x123'
    
    @patch('boto3.client')
    def test_get_secrets_caching(self, mock_boto_client):
        """Test secrets are cached"""
        mock_client = Mock()
        mock_client.get_secret_value.return_value = {
            'SecretString': '{"WALLET_PRIVATE_KEY": "0x123"}'
        }
        mock_boto_client.return_value = mock_client
        
        config = AWSConfig()
        config._secrets_cache = None
        config._secrets_client = None
        
        # First call
        secrets1 = config.get_secrets()
        # Second call should use cache
        secrets2 = config.get_secrets()
        
        assert secrets1 == secrets2
        # Should only call AWS once
        mock_client.get_secret_value.assert_called_once()
    
    @patch('boto3.client')
    def test_get_secrets_missing_key(self, mock_boto_client):
        """Test error when required key is missing"""
        mock_client = Mock()
        mock_client.get_secret_value.return_value = {
            'SecretString': '{"SOME_OTHER_KEY": "value"}'
        }
        mock_boto_client.return_value = mock_client
        
        config = AWSConfig()
        config._secrets_cache = None
        config._secrets_client = None
        
        with pytest.raises(ConfigurationError, match="Missing required secret keys"):
            config.get_secrets()
    
    @patch('boto3.client')
    def test_get_secrets_not_found(self, mock_boto_client):
        """Test error handling for non-existent secret"""
        mock_client = Mock()
        mock_client.get_secret_value.side_effect = ClientError(
            {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Not found'}},
            'GetSecretValue'
        )
        mock_boto_client.return_value = mock_client
        
        config = AWSConfig()
        config._secrets_cache = None
        config._secrets_client = None
        
        with pytest.raises(ConfigurationError, match="not found"):
            config.get_secrets()
    
    def test_clear_cache(self):
        """Test cache clearing"""
        config = AWSConfig()
        config._secrets_cache = {'test': 'value'}
        
        config.clear_cache()
        
        assert config._secrets_cache is None


@pytest.mark.unit
class TestConfigIntegration:
    """Integration tests for configuration"""
    
    def test_get_aws_config_returns_singleton(self):
        """Test get_aws_config returns singleton"""
        config1 = get_aws_config()
        config2 = get_aws_config()
        
        assert config1 is config2
        assert isinstance(config1, AWSConfig)
