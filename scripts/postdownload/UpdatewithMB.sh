#!/bin/bash

# Determine the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Determine the path to the configuration file two levels up
CONF_FILE="$SCRIPT_DIR/../../pdscript.conf"

# Check if the configuration file exists
if [ ! -f "$CONF_FILE" ]; then
    echo "Error: Configuration file not found at $CONF_FILE"
    exit 1
fi

# Extract the path for Music_Download_Folder from the configuration file
MUSIC_DOWNLOAD_FOLDER=$(grep -oP '(?<=Music_Download_Folder=).*' "$CONF_FILE" | sed 's/^[ \t]*//;s/[ \t]*$//')

# Check if the path is not empty
if [ -z "$MUSIC_DOWNLOAD_FOLDER" ]; then
    echo "Error: Music_Download_Folder path is empty or not found in the configuration file."
    exit 1
fi

# Run Picard inside a Docker container, mounting the Music_Download_Folder
docker run --rm \
  -v "$MUSIC_DOWNLOAD_FOLDER":/music_download_folder \
  -e LOAD \
  -e CLUSTER \
  -e LOOKUP \
  -e SAVE_MATCHED \
  -e QUIT \
  mikenye/picard:latest

