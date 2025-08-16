import os
import shutil
import logging
import subprocess
import requests  # For making API calls
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
import configparser
import sys
import time

# Setup logging
script_dir = os.path.dirname(os.path.realpath(__file__))
log_file = os.path.join(script_dir, "music_sorting_debug.log")
# Set up logging to stdout (for INFO/DEBUG) and stderr (for ERROR+)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout)  # All levels to stdout
    ]
)

# Path to the config file (2 folder levels above the script)
config_dir = os.path.abspath(os.path.join(script_dir, "../../"))
config_path = os.path.join(config_dir, 'config.ini')

# Function to read configurations from config.ini
def load_config():
    if not os.path.exists(config_path):
        logging.error(f"config.ini not found at {config_path}. Please ensure it exists.")
        raise FileNotFoundError(f"config.ini not found at {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    # [Paths]
    source_route = config.get('Paths', 'source_route').strip()
    destination_root = config.get('Paths', 'destination_root').strip()
    playlist_dir = config.get('Paths', 'playlist_dir').strip()
    new_artists_dir = config.get('Paths', 'new_artists_dir').strip()
    music_download_folder = config.get('Paths', 'music_download_folder').strip()
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir').strip()
    download_path = music_download_folder  # Formerly from sldl.conf

    # [Jellyfin]
    api_base_url = config.get('Jellyfin', 'api_base_url').strip()
    api_auth_token = config.get('Jellyfin', 'api_auth_token').strip()
    user_id = config.get('Jellyfin', 'user_id').strip()
    main_music_library_id = config.get('Jellyfin', 'main_music_library_id').strip()

    logging.debug(f"Config loaded: source_route={source_route}, destination_root={destination_root}, "
                  f"new_artists_dir={new_artists_dir}, music_download_folder={music_download_folder}, "
                  f"unknown_albums_dir={unknown_albums_dir}, api_base_url={api_base_url}")

    return (source_route, destination_root, new_artists_dir, music_download_folder, 
            unknown_albums_dir, api_base_url, api_auth_token, user_id, 
            playlist_dir, download_path, main_music_library_id)

# Load configuration variables
(source_route, destination_root, new_artists_dir, music_download_folder, 
 unknown_albums_dir, api_base_url, api_auth_token, user_id, 
 playlist_dir, download_path, main_music_library_id) = load_config()

# Audio file extensions to check
audio_extensions = {'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.wav', '.ogg', '.wma', '.alac', '.aiff', '.opus'}

# API headers
HEADERS = {
    'Authorization': f'MediaBrowser Token={api_auth_token}',
    'accept': 'application/json',
    'Content-Type': 'application/json'
}

# Function to set permissions
def set_permissions(path):
    try:
        os.chmod(path, 0o777)
        logging.info(f"Set permissions to 777 for {path}")
    except Exception as e:
        logging.error(f"Failed to set permissions for {path}: {e}")

# Function to check for audio files and .incomplete files in a folder
def folder_contains_audio_or_incomplete(folder_path):
    contains_audio = False
    contains_incomplete = False
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_ext = os.path.splitext(file)[1].lower()
            if file_ext in audio_extensions:
                contains_audio = True
            if file_ext == '.incomplete':
                contains_incomplete = True
            if contains_incomplete:
                break  # If .incomplete file found, break early
        if contains_incomplete:
            break
    return contains_audio, contains_incomplete

# Function to move files and check size
def move_and_compare(src_file, dst_file):
    logging.debug(f"Comparing source file: {src_file} with destination file: {dst_file}")
    src_size = os.path.getsize(src_file)
    if os.path.exists(dst_file):
        dst_size = os.path.getsize(dst_file)
        logging.debug(f"Source file size: {src_size}, Destination file size: {dst_size}")
        if src_size > dst_size:
            os.remove(dst_file)
            shutil.move(src_file, dst_file)
            logging.info(f"Moved larger file from {src_file} to {dst_file}")
        else:
            os.remove(src_file)
            logging.info(f"Deleted smaller file {src_file} because {dst_file} is larger")
    else:
        shutil.move(src_file, dst_file)
        logging.info(f"Moved file {src_file} to {dst_file}")

# Function to move folders with audio files to unknown album folder
def move_folders_with_audio_to_unknown():
    logging.info(f"Checking folders in music_download_folder: {music_download_folder}")

    for folder in os.listdir(music_download_folder):
        folder_path = os.path.join(music_download_folder, folder)

        if os.path.isdir(folder_path):
            logging.info(f"Processing folder: {folder_path}")

            delete_specific_files_in_all_subdirectories(folder_path)

            if not os.listdir(folder_path):
                logging.info(f"Folder {folder_path} is empty after deleting specific files. Removing folder.")
                shutil.rmtree(folder_path)
                continue

            contains_audio, contains_incomplete = folder_contains_audio_or_incomplete(folder_path)

            if contains_incomplete:
                logging.info(f"Skipping folder {folder_path} because it contains .incomplete files.")
                continue

            if contains_audio:
                destination_folder = None
                # Check for playlist marker
                if os.path.exists(os.path.join(folder_path, '.is_playlist')):
                    logging.info(f"Playlist folder detected: {folder}. Moving to destination root.")
                    destination_folder = os.path.join(destination_root, folder)
                    # Clean up marker file before moving
                    os.remove(os.path.join(folder_path, '.is_playlist'))
                else:
                    logging.info(f"Album/single detected: {folder}. Moving to unknown album folder for sorting.")
                    destination_folder = os.path.join(unknown_albums_dir, folder)

                try:
                    logging.info(f"Moving folder {folder_path} to {destination_folder}")
                    if os.path.exists(destination_folder):
                         logging.warning(f"Destination {destination_folder} already exists. Merging contents.")
                         shutil.copytree(folder_path, destination_folder, dirs_exist_ok=True)
                         shutil.rmtree(folder_path)
                    else:
                         shutil.move(folder_path, destination_folder)
                except (shutil.Error, OSError) as e:
                    logging.error(f"Error moving folder {folder_path} to {destination_folder}: {e}")
            else:
                logging.info(f"Skipping folder {folder_path} because it contains no audio files.")
        else:
            logging.info(f"Skipping non-directory item: {folder_path}")

# Function to update metadata using mutagen
def update_metadata(file_path, genre, album_artist):
    logging.debug(f"Updating metadata for {file_path}")
    try:
        if file_path.endswith('.mp3'):
            audio = EasyID3(file_path)
            audio['genre'] = genre
            audio['albumartist'] = album_artist
            audio.save()
        elif file_path.endswith('.flac'):
            audio = FLAC(file_path)
            audio['genre'] = genre
            audio['albumartist'] = album_artist
            audio.save()
        elif file_path.endswith('.m4a') or file_path.endswith('.mp4'):
            audio = MP4(file_path)
            audio['\xa9gen'] = genre
            audio['aART'] = album_artist
            audio.save()
        else:
            logging.warning(f"Unsupported file format: {file_path}")
            return False
        logging.info(f"Updated metadata for {file_path}: Genre = {genre}, Album Artist = {album_artist}")
        return True
    except Exception as e:
        logging.error(f"Error updating metadata for {file_path}: {e}")
        return False

# Function to make GET request and fetch artist details
def fetch_artist_info(artist_name):
    logging.debug(f"Fetching artist info for {artist_name}")
    try:
        url = f"{api_base_url}/Artists/{artist_name}"
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 200:
            artist_info = response.json()
            logging.info(f"Successfully fetched artist info for {artist_name}")
            return artist_info
        else:
            logging.error(f"Failed to fetch artist info for {artist_name}: Status {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Error fetching artist info for {artist_name}: {e}")
        return None

# Function to refresh artist metadata using the artist ID
def refresh_artist_metadata(artist_id):
    logging.debug(f"Refreshing metadata for artist ID {artist_id}")
    try:
        url = f"{api_base_url}/Items/{artist_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
        response = requests.post(url, headers=HEADERS)
        if response.status_code == 204:
            logging.info(f"Successfully refreshed metadata for artist ID {artist_id}")
        else:
            logging.error(f"Failed to refresh metadata for artist ID {artist_id}: Status {response.status_code}")
    except Exception as e:
        logging.error(f"Error refreshing metadata for artist ID {artist_id}: {e}")

# Function to move contents of one folder to another, merging if necessary
def move_folder_contents(src, dst):
    """Move contents of src to dst, merging if dst already exists."""
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.isdir(src_path):
            # Recursively merge directories
            move_folder_contents(src_path, dst_path)
        else:
            # Move files, but replace if the file in dst is smaller
            move_and_compare(src_path, dst_path)

# Function to process each artist folder
def process_artist_folder(artist_folder):
    artist_name = os.path.basename(artist_folder)
    logging.info(f"Processing artist folder: {artist_name}")

    # Recursively search for the artist in the destination root
    match_found = False
    for root, dirs, files in os.walk(destination_root):
        if artist_name in dirs:
            destination_artist_folder = os.path.join(root, artist_name)
            genre_folder = os.path.basename(os.path.dirname(destination_artist_folder))
            logging.info(f"Found matching artist folder: {destination_artist_folder} with Genre: {genre_folder}")
            match_found = True

            # Move all files and subfolders from source to destination
            for src_root, _, src_files in os.walk(artist_folder):
                dst_root = src_root.replace(artist_folder, destination_artist_folder, 1)
                os.makedirs(dst_root, exist_ok=True)

                # Set permissions for the destination folder
                set_permissions(dst_root)

                for file in src_files:
                    src_file = os.path.join(src_root, file)
                    dst_file = os.path.join(dst_root, file)

                    # Move and compare files based on size
                    move_and_compare(src_file, dst_file)

                    # Update metadata for supported formats
                    update_metadata(dst_file, genre_folder, artist_name)

            # Make API calls to fetch artist info and refresh metadata
            artist_info = fetch_artist_info(artist_name)
            if artist_info and 'Id' in artist_info:
                refresh_artist_metadata(artist_info['Id'])

            break

    if not match_found:
        # No matching artist folder found, move to new artists directory
        new_artist_folder = os.path.join(new_artists_dir, artist_name)
        logging.info(f"No match found, moving {artist_folder} to new artists directory: {new_artist_folder}")

        # If the new_artist_folder already exists, merge its contents
        if os.path.exists(new_artist_folder):
            logging.info(f"Destination folder {new_artist_folder} already exists. Merging contents.")
            move_folder_contents(artist_folder, new_artist_folder)
            # Remove the source folder after merging
            shutil.rmtree(artist_folder)
        else:
            shutil.move(artist_folder, new_artist_folder)

# Standalone function to delete specific files (.cue, .log, .m3u) in a directory and its subdirectories
def delete_specific_files_in_all_subdirectories(directory):
    logging.info(f"Deleting specific files (.cue, .log, .m3u) in: {directory}")
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(('.cue', '.log', '.m3u')):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    logging.info(f"Successfully deleted file: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting file {file_path}: {e}")

# Function to delete empty directories after all processing is complete
def cleanup_empty_directories(directory):
    logging.info(f"Deleting empty directories in: {directory}")
    for root, dirs, _ in os.walk(directory, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            if not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                    logging.info(f"Successfully deleted empty directory: {dir_path}")
                except Exception as e:
                    logging.error(f"Error deleting empty directory {dir_path}: {e}")

def get_jellyfin_item_counts(api_base_url, headers):
    """Get item counts from Jellyfin."""
    url = f"{api_base_url}/Items/counts"
    try:
        logging.debug(f"Requesting item counts from URL: {url}")
        logging.debug(f"Using headers: {headers}")
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Failed to get item counts: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logging.error(f"Exception while getting item counts: {e}")
        return None

def get_jellyfin_audio_library(api_base_url, headers, user_id, main_music_library_id):
    """Get all audio items from Jellyfin with their file paths"""
    logging.info("Fetching audio library from Jellyfin...")
    
    try:
        # Get all audio items with their file paths
        url = f"{api_base_url}/Users/{user_id}/Items"
        params = {
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Fields": "Path",  # Include the file path
            "Limit": 50000  # High limit to get all songs
        }
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            items = response.json().get('Items', [])
            
            # Create a mapping of filename -> item ID
            filename_to_id = {}
            for item in items:
                if 'Path' in item:
                    filename = os.path.basename(item['Path'])
                    filename_to_id[filename] = item['Id']
            
            logging.info(f"Loaded {len(filename_to_id)} audio files from Jellyfin")
            return filename_to_id
            
        else:
            logging.error(f"Failed to fetch audio library: {response.status_code} - {response.text}")
            return {}
            
    except Exception as e:
        logging.error(f"Exception while fetching audio library: {e}")
        return {}

def trigger_library_scan(api_base_url, headers, main_music_library_id):
    """Trigger a Jellyfin library scan"""
    url = f"{api_base_url}/Items/{main_music_library_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
    
    try:
        response = requests.post(url, headers=headers)
        if response.status_code < 300:
            logging.info('Jellyfin Library Scan triggered successfully')
            return True
        else:
            logging.error(f"Failed to trigger library scan: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logging.error(f"Exception while triggering library scan: {e}")
        return False

def wait_for_library_scan_completion(api_base_url, headers, expected_song_count, max_wait_time=300, check_interval=10):
    """Wait for library scan to complete by polling the song count."""
    logging.info(f"Waiting for Jellyfin song count to reach at least {expected_song_count}...")
    
    start_time = time.time()
    while time.time() - start_time < max_wait_time:
        counts = get_jellyfin_item_counts(api_base_url, headers)
        if counts and 'SongCount' in counts:
            current_song_count = counts['SongCount']
            logging.info(f"Current song count: {current_song_count} / {expected_song_count}")
            if current_song_count >= expected_song_count:
                logging.info("Expected song count reached. Library scan appears to be complete.")
                return True
        else:
            logging.warning("Could not retrieve current song count from Jellyfin.")
            
        time.sleep(check_interval)
    
    logging.warning(f"Timed out waiting for library scan after {max_wait_time} seconds.")
    final_counts = get_jellyfin_item_counts(api_base_url, headers)
    final_song_count = final_counts.get('SongCount', 'N/A') if final_counts else 'N/A'
    logging.warning(f"Final song count was {final_song_count}, but expected {expected_song_count}.")
    return False

def create_jellyfin_playlist(playlist_name, song_files, api_base_url, headers, user_id, main_music_library_id, max_retries=3):
    """Create a Jellyfin playlist from a given list of song filenames with retry logic."""
    logging.info(f"Attempting to create Jellyfin playlist for: {playlist_name}")

    if not song_files:
        logging.warning(f"No song files provided for playlist '{playlist_name}'.")
        return None

    song_ids = []
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logging.info(f"Retry attempt {attempt}/{max_retries} for playlist '{playlist_name}'...")
            time.sleep(10) # Wait before retrying

        # Get the Jellyfin audio library mapping (filename → item ID)
        filename_to_id = get_jellyfin_audio_library(api_base_url, headers, user_id, main_music_library_id)

        if not filename_to_id and attempt < max_retries:
            logging.warning("Could not load Jellyfin audio library. Will retry.")
            continue
        
        # Match songs by filename
        song_ids = []
        found_songs = []
        not_found_songs = []

        for song_file in song_files:
            if song_file in filename_to_id:
                song_id = filename_to_id[song_file]
                song_ids.append(song_id)
                found_songs.append(song_file)
            else:
                not_found_songs.append(song_file)

        logging.info(f"Found {len(found_songs)}/{len(song_files)} songs in the library.")
        if not_found_songs:
            logging.warning(f"Could not find: {', '.join(not_found_songs)}")

        # If we found all songs, we can break early.
        if not not_found_songs:
            break
        
        # If we are on the last attempt, we proceed with what we have.
        if attempt == max_retries:
            break

    if not song_ids:
        logging.error(f"No songs found in Jellyfin for playlist '{playlist_name}' after {max_retries + 1} attempts. Playlist will not be created.")
        return None

    # Create the playlist
    try:
        create_playlist_url = f"{api_base_url}/Playlists"
        payload = {
            "Name": playlist_name,
            "UserId": user_id,
            "Ids": song_ids
        }

        response = requests.post(create_playlist_url, headers=headers, json=payload)

        if response.status_code == 200:
            playlist_data = response.json()
            playlist_id = playlist_data['Id']
            logging.info(f"✓ Successfully created playlist '{playlist_name}' with {len(song_ids)} songs (ID: {playlist_id})")
            if not_found_songs:
                logging.warning(f"Note: {len(not_found_songs)} songs were not found in Jellyfin and were excluded from the playlist.")
            return playlist_id
        else:
            logging.error(f"Failed to create playlist '{playlist_name}': {response.status_code} - {response.text}")
            return None

    except Exception as e:
        logging.error(f"Exception while creating playlist '{playlist_name}': {e}")
        return None

def move_playlist_folders():
    music_extensions = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.wma'}

    download_path_clean = str(download_path).strip('"').strip("'").rstrip('/')
    logging.info(f"Cleaned download_path: {repr(download_path_clean)}")

    if not os.path.exists(download_path_clean):
        logging.error(f"Download path does not exist: {download_path_clean}")
        return

    # Get initial song count
    initial_counts = get_jellyfin_item_counts(api_base_url, HEADERS)
    if not initial_counts:
        logging.error("Could not get initial item counts from Jellyfin. Aborting playlist processing.")
        return
    initial_song_count = initial_counts.get('SongCount', 0)
    logging.info(f"Initial song count in Jellyfin: {initial_song_count}")

    files_in_download_path = os.listdir(download_path_clean)
    logging.info(f"Checking for playlist folders in: {download_path_clean}, found {len(files_in_download_path)} items")    

    total_files_moved = 0
    playlists_to_create = []  # List of tuples: (playlist_name, music_files, original_item_path)

    # --- First Pass: Move files and collect data ---
    for item in files_in_download_path:
        item_path = os.path.join(download_path_clean, item)
        if not os.path.isdir(item_path):
            continue

        try:
            all_files = os.listdir(item_path)
            music_files = [f for f in all_files if os.path.splitext(f)[1].lower() in music_extensions]
        except Exception as e:
            logging.error(f"Error reading directory {item_path}: {e}")
            continue

        if music_files:
            playlist_name = item
            destination_folder = os.path.join(playlist_dir, playlist_name)
            logging.info(f"Found playlist '{playlist_name}' with {len(music_files)} songs.")

            try:
                if os.path.exists(destination_folder):
                    logging.warning(f"Playlist folder {destination_folder} already exists. Merging music files.")
                    move_folder_contents(item_path, destination_folder)
                else:
                    os.makedirs(destination_folder, exist_ok=True)
                    for file in music_files:
                        shutil.move(os.path.join(item_path, file), destination_folder)
                
                logging.info(f"Moved {len(music_files)} music files to: {destination_folder}")
                total_files_moved += len(music_files)
                playlists_to_create.append((playlist_name, music_files, item_path))

            except Exception as e:
                logging.error(f"Error moving music files from {item_path}: {e}")

    # --- Second Pass: Scan and create playlists if files were moved ---
    if total_files_moved > 0:
        logging.info(f"Moved a total of {total_files_moved} files. Triggering a single library scan.")
        
        if trigger_library_scan(api_base_url, HEADERS, main_music_library_id):
            expected_song_count = initial_song_count + total_files_moved
            scan_completed = wait_for_library_scan_completion(api_base_url, HEADERS, expected_song_count)

            if scan_completed:
                logging.info("Library scan completed. Creating playlists...")
                for playlist_name, music_files, original_item_path in playlists_to_create:
                    playlist_id = create_jellyfin_playlist(
                        playlist_name, music_files, api_base_url, HEADERS, user_id, main_music_library_id
                    )
                    if playlist_id:
                        logging.info(f"Successfully created Jellyfin playlist '{playlist_name}' with ID: {playlist_id}")
                        if os.path.exists(original_item_path):
                            try:
                                shutil.rmtree(original_item_path, ignore_errors=True)
                                logging.info(f"✓ Cleaned up leftover downloaded folder: {original_item_path}")
                            except Exception as e:
                                logging.error(f"Failed to remove leftover folder {original_item_path}: {e}")
                    else:
                        logging.error(f"Failed to create Jellyfin playlist for '{playlist_name}' after successful scan.")
            else:
                logging.error("Library scan did not complete as expected. Skipping playlist creation.")
        else:
            logging.error("Failed to trigger library scan. Skipping playlist creation.")
    else:
        logging.info("No new playlist files to move.")
                    
# Main function to iterate through all artist folders in the source root
def main():
    logging.info(f"Starting the music sorting script with source_route: {source_route}")

    # Move playlist folders
    move_playlist_folders()

    return

    # Delete specific files (.cue, .log, .m3u) in relevant directories before any other processing
    delete_specific_files_in_all_subdirectories(source_route)
    delete_specific_files_in_all_subdirectories(music_download_folder)
    delete_specific_files_in_all_subdirectories(unknown_albums_dir)

    # Make sure the new artist directory exists
    if not os.path.exists(new_artists_dir):
        os.makedirs(new_artists_dir)
        logging.info(f"Created new artists directory: {new_artists_dir}")
    else:
        logging.info(f"New artists directory already exists: {new_artists_dir}")

    # Log all items in source_route
    items = os.listdir(source_route)
    logging.info(f"Items in source_route: {items}")

    # Process each artist folder in source_route
    for item in items:
        artist_folder = os.path.join(source_route, item)
        logging.info(f"Processing item: {item}")

        if os.path.isdir(artist_folder):
            logging.info(f"Found artist folder: {artist_folder}, starting process_artist_folder")
            process_artist_folder(artist_folder)
        else:
            logging.info(f"Skipping non-directory item: {artist_folder}")

    # After processing artist folders, move folders with audio files to unknown album folder
    move_folders_with_audio_to_unknown()

    # Move playlist folders
    # move_playlist_folders()

    # Run the empty directory cleanup after processing everything else at source
    cleanup_empty_directories(source_route)

    # Run the empty directory cleanup after processing everything else at download folder
    cleanup_empty_directories(music_download_folder)
    
    # Run the empty directory cleanup after processing everything else at unknown album folder
    cleanup_empty_directories(unknown_albums_dir)

if __name__ == "__main__":
    main()
