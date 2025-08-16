#!/bin/bash
# curl-wrapper.sh

# Log file
LOGFILE="/app/curl.log"

# Log the full command and timestamp
echo "$(date '+%Y-%m-%d %H:%M:%S') - Called: curl $*" >> "$LOGFILE"

# Run the real curl
/usr/bin/curl "$@"