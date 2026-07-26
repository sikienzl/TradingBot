"""
SecretManager: Centralized system for accessing all sensitive credentials.

This module acts as the single gatekeeper for secrets, eliminating reliance on 
environment variables or hardcoded keys. All services must use its methods 
to retrieve any secret data (e.g., API keys, database passwords).

NOTE: This implementation uses a placeholder dictionary for demonstration purposes.
In production, this class should integrate with dedicated vault solutions 
(e.g., HashiCorp Vault, AWS Secrets Manager, Azure Key Vault) using their SDKs.
"""

import os
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod

class SecretManager(ABC):
    """Abstract base class for secret management."""
    
    @abstractmethod
    def get_secret(self, key: str) -> Optional[str]:
        """Retrieve a secret by key."""
        pass
    
    @abstractmethod
    def initialize(self) -> None:
        """Initialize the secret manager."""
        pass

class VaultSecretManager(SecretManager):
    """Production implementation using HashiCorp Vault."""
    
    def __init__(self, vault_url: str, token: str):
        self.vault_url = vault_url
        self.token = token
        self._client = None
        
    def initialize(self) -> None:
        """Initialize connection to Vault."""
        try:
            from hvac import Client
            self._client = Client(url=self.vault_url)
            self._client.token = self.token
            # Verify connection
            self._client.sys.read_healthy_status()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Vault client: {e}")
    
    def get_secret(self, key: str) -> Optional[str]:
        """Retrieve secret from Vault."""
        if not self._client:
            raise RuntimeError("SecretManager not initialized")
            
        try:
            secret_data = self._client.secrets.kv.v2.read_secret_version(
                path=key, 
                mount_point="secret"
            )
            return secret_data['data']['data'].get('value')
        except Exception as e:
            print(f"Failed to retrieve secret {key}: {e}")
            return None

class MockSecretManager(SecretManager):
    """Development/testing implementation."""
    
    def __init__(self, secrets: Dict[str, str] = None):
        self.secrets = secrets or {}
        
    def initialize(self) -> None:
        """Initialize mock manager."""
        pass
        
    def get_secret(self, key: str) -> Optional[str]:
        """Retrieve secret from mock store."""
        return self.secrets.get(key)

# Global instance
_secret_manager: Optional[SecretManager] = None

def get_secret_manager() -> SecretManager:
    """Get the configured secret manager."""
    global _secret_manager
    
    if _secret_manager is None:
        # Check environment for production configuration
        vault_url = os.getenv("VAULT_URL")
        vault_token = os.getenv("VAULT_TOKEN")
        
        if vault_url and vault_token:
            _secret_manager = VaultSecretManager(vault_url, vault_token)
        else:
            # Fallback to mock for development
            mock_secrets = {
                "postgres/db/password": "secure-db-pass-123",
                "api/external_key": "xyz_super_secret_api_key",
                "service/redis_url": "redis://mock:6379/0"
            }
            _secret_manager = MockSecretManager(mock_secrets)
            
        _secret_manager.initialize()
    
    return _secret_manager

def get_secret(key: str) -> Optional[str]:
    """Get a secret using the configured manager."""
    manager = get_secret_manager()
    return manager.get_secret(key)

# Example usage snippet (should be removed from the final library file)
if __name__ == '__main__':
    # Initialize and test the secret manager
    manager = get_secret_manager()
    
    db_pass = get_secret("postgres/db/password")
    api_key = get_secret("api/external_key")

    if db_pass:
        print(f"\nSuccessfully retrieved DB Password (first 4 chars): {db_pass[:4]}...")
    else:
        print("\nCould not retrieve database password.")