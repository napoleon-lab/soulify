#!/bin/bash

# on_complete_handler.sh

# Take command_id as an argument
COMMAND_ID=$1

# Path to your configuration file (same directory as this script)
SCRIPT_DIR="$(dirname "$0")"
CONFIG_FILE="$SCRIPT_DIR/spotifyauth.conf"

# Check if the configuration file exists
if [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: Configuration file not found at $CONFIG_FILE"
  exit 1
fi

# Read the redirect-uri value from the configuration file using awk
REDIRECT_URI=$(awk -F' = ' '/^redirect-uri/ {print $2}' "$CONFIG_FILE")

# Check if REDIRECT_URI is not empty
if [ -z "$REDIRECT_URI" ]; then
  echo "Error: redirect-uri not found or is empty in $CONFIG_FILE"
  exit 1
fi

# Replace the final occurrence of 'callback' in the URL with 'terminate_command/COMMAND_ID'
TERMINATE_URL="${REDIRECT_URI%/callback}/terminate_command/$COMMAND_ID"

# Send a POST request to terminate the command gracefully
curl -X POST "$TERMINATE_URL"

# Optional: Add any other clean-up logic here, such as killing additional processes
# pkill sldl
