"""
Configuration Loader

This module handles loading configuration files for the trading bot.
"""

import json
import yaml
import os
from typing import Dict, Any, Optional
import logging

class ConfigLoader:
    """Class for loading and managing configuration."""
    
    def __init__(self):
        """Initialize the config loader."""
        self.logger = logging.getLogger(__name__)
        self.logger.info("Config loader initialized")
        self.config = {}
    
    def load_config(self, config_path: str) -> Dict[str, Any]:
        """
        Load configuration from file.
        
        Args:
            config_path: Path to config file
            
        Returns:
            Configuration dictionary
        """
        self.logger.info(f"Loading config from {config_path}")
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r') as f:
            if config_path.endswith('.json'):
                self.config = json.load(f)
            elif config_path.endswith(('.yaml', '.yml')):
                self.config = yaml.safe_load(f)
            else:
                raise ValueError(f"Unsupported config file format: {config_path}")
        
        return self.config
    
    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.
        
        Args:
            key: Configuration key
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        return self.config.get(key, default)
    
    def get_nested_config(self, keys: list, default: Any = None) -> Any:
        """
        Get a nested configuration value.
        
        Args:
            keys: List of keys for nested access
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        value = self.config
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default

# Example usage
if __name__ == "__main__":
    loader = ConfigLoader()
    print("Config loader initialized")