"""
Configuration Loader

This module handles loading and managing the centralized configuration for the trading bot.
"""

import os
import json
import yaml
from typing import Dict, Any, Optional
from pathlib import Path
import logging

from .schema import BotConfig


class ConfigLoader:
    """Class for loading and managing centralized configuration."""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the config loader.
        
        Args:
            config_path: Path to config file (optional, will use default if not provided)
        """
        self.logger = logging.getLogger(__name__)
        self.config_path = config_path
        self.config = BotConfig()
        self.logger.info("Centralized config loader initialized")
    
    def load_from_file(self, config_path: str) -> BotConfig:
        """
        Load configuration from a file.
        
        Args:
            config_path: Path to config file
            
        Returns:
            Loaded configuration object
        """
        self.logger.info(f"Loading config from {config_path}")
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r') as f:
            if config_path.endswith('.json'):
                config_data = json.load(f)
            elif config_path.endswith(('.yaml', '.yml')):
                config_data = yaml.safe_load(f)
            else:
                raise ValueError(f"Unsupported config file format: {config_path}")
        
        # Apply loaded configuration
        self._apply_config_data(config_data)
        return self.config
    
    def load_from_env(self) -> BotConfig:
        """
        Load configuration from environment variables.
        
        Returns:
            Loaded configuration object
        """
        self.logger.info("Loading config from environment variables")
        # Configuration is automatically loaded in BotConfig.__post_init__
        return self.config
    
    def _apply_config_data(self, config_data: Dict[str, Any]) -> None:
        """Apply configuration data to the current config object."""
        # This method would be expanded to fully populate all config sections
        # For now, we'll rely on the dataclass defaults and environment loading
        
        # Set basic environment settings
        if 'environment' in config_data:
            self.config.environment = config_data['environment']
        
        if 'simulate_data' in config_data:
            self.config.simulate_data = config_data['simulate_data']
    
    def save_to_file(self, config_path: str) -> None:
        """
        Save current configuration to a file.
        
        Args:
            config_path: Path to save config file
        """
        self.logger.info(f"Saving config to {config_path}")
        
        # Convert config to dictionary
        config_dict = self.config.to_dict()
        
        # Write to file (determine format from extension)
        if config_path.endswith('.json'):
            with open(config_path, 'w') as f:
                json.dump(config_dict, f, indent=2)
        elif config_path.endswith(('.yaml', '.yml')):
            with open(config_path, 'w') as f:
                yaml.safe_dump(config_dict, f, default_flow_style=False)
        else:
            raise ValueError(f"Unsupported config file format: {config_path}")
    
    def get_config(self) -> BotConfig:
        """
        Get the current configuration.
        
        Returns:
            Current configuration object
        """
        return self.config
    
    def update_config(self, updates: Dict[str, Any]) -> None:
        """
        Update configuration with new values.
        
        Args:
            updates: Dictionary of configuration updates
        """
        self.logger.info("Updating configuration")
        # This would be implemented to update specific config values
        pass


def get_default_config() -> BotConfig:
    """
    Get the default configuration.
    
    Returns:
        Default configuration object
    """
    return BotConfig()


def load_config(config_path: Optional[str] = None) -> BotConfig:
    """
    Load configuration from file or environment.
    
    Args:
        config_path: Path to config file (optional)
        
    Returns:
        Loaded configuration object
    """
    loader = ConfigLoader(config_path)
    
    # Try to load from file first, then fallback to environment
    if config_path and os.path.exists(config_path):
        return loader.load_from_file(config_path)
    else:
        return loader.load_from_env()