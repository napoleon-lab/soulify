import re
import configparser
import os
import requests
from config_manager import SLDL_CONFIG_PATH, get_setting

def clean_special_chars(query):
    if '-' in query:
        query = query.split('-', 1)[0]  # Split at the first hyphen and keep the part before it
    query = query.strip()
    return re.sub(r'[^a-zA-Z0-9\s]', '', query)

# Function to read SoulifyURL.conf
def get_base_url():
    try:
        base_url = get_setting('Server', 'base_url')
        if base_url.endswith('/'):
            base_url = base_url[:-1]  # Remove trailing slash if present
        print(f"Parsed URL from config: {base_url}")
        
        try:
            requests.get(base_url, timeout=5, verify=False)
        except Exception as e:
            print(f"base url is not reachable: {e}")
            raise e

        print(f"Base URL was verified.")

        return base_url
    except configparser.Error as e:
        print(f"Error reading config: {e}")
        return 'http://localhost:5000'  # Fallback to default localhost

# Corrected command construction function
def construct_track_download_command(sldlPath, artistName, albumName, trackName, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    cleaned_album_name = clean_special_chars(albumName)
    cleaned_track_name = clean_special_chars(trackName)  # Corrected variable assignment

    base_url = get_base_url()
    on_complete_url = f"{base_url}/complete_download/{command_id}"

    return (f'"{sldlPath}" "artist={cleaned_artist_name}, album={cleaned_album_name}, title={cleaned_track_name}" '
            f'--album --interactive --strict-title --config "{SLDL_CONFIG_PATH}" --debug --yt-dlp --on-complete "curl -kX POST {on_complete_url}"')


def construct_album_download_command(sldlPath, artistName, albumName, totalTracks, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    cleaned_album_name = clean_special_chars(albumName)
    base_url = get_base_url()
    on_complete_url = f"{base_url}/complete_download/{command_id}"
    return (f'"{sldlPath}" "artist={cleaned_artist_name}, album={cleaned_album_name}" --album-track-count {totalTracks} '
            f'--album --interactive --config "{SLDL_CONFIG_PATH}" --debug --yt-dlp --on-complete "curl -kX POST {on_complete_url}"')

def construct_artist_download_command(sldlPath, artistName, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    base_url = get_base_url()
    on_complete_url = f"{base_url}/complete_download/{command_id}"
    return (f'"{sldlPath}" "artist={cleaned_artist_name}" '
            f'--aggregate --album --interactive --config "{SLDL_CONFIG_PATH}" '
            f'--debug --yt-dlp --on-complete "curl -kX POST {on_complete_url}"')

def construct_playlist_download_command(sldlPath, playlistID, command_id):
    base_url = get_base_url()
    on_complete_url = f"{base_url}/complete_download/{command_id}"

    print(f"URL on download complete: {on_complete_url}")
    return (f'"{sldlPath}" "https://open.spotify.com/playlist/{playlistID}" '
            f'--config "{SLDL_CONFIG_PATH}" --no-remove-special-chars '
            f'--debug --write-playlist --yt-dlp --on-complete "curl -kX POST {on_complete_url}"')

