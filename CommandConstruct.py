import re
import configparser
import os

def clean_special_chars(query):
    if '-' in query:
        query = query.split('-', 1)[0]  # Split at the first hyphen and keep the part before it
    query = query.strip()
    return re.sub(r'[^a-zA-Z0-9\s]', '', query)

# Function to read SoulifyURL.conf
def read_soulify_url_conf():
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), 'SoulifyURL.conf')
    config.read(config_path)
    try:
        return config.get('Server', 'base_url')
    except configparser.Error as e:
        print(f"Error reading SoulifyURL.conf: {e}")
        return 'http://localhost:5000'  # Fallback to default localhost

# Corrected command construction function
def construct_track_download_command(sldlPath, sldlConfigPath, artistName, albumName, trackName, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    cleaned_album_name = clean_special_chars(albumName)
    cleaned_track_name = clean_special_chars(trackName)  # Corrected variable assignment

    base_url = read_soulify_url_conf()
    on_complete_url = f"{base_url}/complete_download/{command_id}"

    return (f'"{sldlPath}" "artist={cleaned_artist_name}, album={cleaned_album_name}, title={cleaned_track_name}" '
            f'--album --interactive --strict-title --config "{sldlConfigPath}" --on-complete "curl -X POST {on_complete_url}"')


def construct_album_download_command(sldlPath, sldlConfigPath, artistName, albumName, totalTracks, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    cleaned_album_name = clean_special_chars(albumName)
    base_url = read_soulify_url_conf()
    on_complete_url = f"{base_url}/complete_download/{command_id}"
    return (f'"{sldlPath}" "artist={cleaned_artist_name}, album={cleaned_album_name}" --album-track-count {totalTracks} '
            f'--album --interactive --config "{sldlConfigPath}" --on-complete "curl -X POST {on_complete_url}"')

def construct_artist_download_command(sldlPath, sldlConfigPath, artistName, command_id):
    cleaned_artist_name = clean_special_chars(artistName)
    base_url = read_soulify_url_conf()
    on_complete_url = f"{base_url}/complete_download/{command_id}"
    return (f'"{sldlPath}" "artist={cleaned_artist_name}" '
            f'--aggregate --album --interactive --config "{sldlConfigPath}" '
            f'--on-complete "curl -X POST {on_complete_url}"')

def construct_playlist_download_command(sldlPath, sldlConfigPath, playlistID, command_id):
    base_url = read_soulify_url_conf()
    on_complete_url = f"{base_url}/complete_download/{command_id}"
    return (f'"{sldlPath}" "https://open.spotify.com/playlist/{playlistID}" '
            f'--config "{sldlConfigPath}" --no-remove-special-chars '
            f'--on-complete "curl -X POST {on_complete_url}"')
