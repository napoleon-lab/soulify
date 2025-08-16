import configparser
import os

# Path to the main configuration file
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
SLDL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sldl.conf')

def get_config():
    """Reads the main config.ini file and returns a ConfigParser object."""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config

def get_setting(section, key, fallback=None):
    """
    Gets a setting value.
    It first checks for an environment variable, then falls back to the config file.
    Environment variable name should be in the format SOULIFY_{SECTION}_{KEY}.
    """
    env_var = f"SOULIFY_{section.upper()}_{key.upper()}"
    value = os.getenv(env_var)
    if value is not None:
        return value
    
    config = get_config()
    return config.get(section, key, fallback=fallback)

def write_config(config):
    """Writes the ConfigParser object to the main config.ini file."""
    with open(CONFIG_PATH, 'w') as configfile:
        config.write(configfile)

def generate_sldl_config():
    """
    Generates the sldl.conf file from the main config.ini.
    It takes all keys from the [sLDL] section and adds credentials.
    """
    config = get_config()
    
    with open(SLDL_CONFIG_PATH, 'w') as f:
        f.write("# This file is auto-generated from config.ini. Do not edit manually.\n\n")
        
        # Add credentials and required settings
        f.write("# Soulseek Credentials (required)\n")
        f.write(f"username = {config.get('Soulseek', 'username', fallback='')}\n")
        f.write(f"password = {config.get('Soulseek', 'password', fallback='')}\n")
        f.write("\n# General Download Settings\n")
        f.write(f"path = \"{config.get('Paths', 'music_download_folder', fallback='/app/downloads/')}\"\n")
        f.write("\n# Spotify Settings\n")
        f.write(f"spotify-id = {config.get('Spotify', 'client_id', fallback='')}\n")
        f.write(f"spotify-secret = {config.get('Spotify', 'client_secret', fallback='')}\n")

        # Add all other settings from the [sLDL] section
        if config.has_section('sLDL'):
            f.write("\n# sLDL Specific Settings\n")
            for key, value in config.items('sLDL'):
                f.write(f"{key} = {value}\n")
