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

    logging.debug(f"Config loaded: source_route={source_route}, destination_root={destination_root}, "
                  f"new_artists_dir={new_artists_dir}, music_download_folder={music_download_folder}, "
                  f"unknown_albums_dir={unknown_albums_dir}")

    return (source_route, destination_root, new_artists_dir, music_download_folder, 
            unknown_albums_dir, playlist_dir, download_path)

# Load configuration variables
(source_route, destination_root, new_artists_dir, music_download_folder, 
 unknown_albums_dir, playlist_dir, download_path) = load_config()

# Audio file extensions to check
audio_extensions = {'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.wav', '.ogg', '.wma', '.alac', '.aiff', '.opus'}

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

# Function to move contents of one folder to another, merging if necessary
def move_folder_contents(src, dst, extensions=None):
    """
    Move contents of src to dst, merging if dst already exists.
    If `extensions` is provided, only files with those extensions are moved.
    Returns the number of newly added files.
    """
    new_files_count = 0
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.isdir(src_path):
            # Recursively merge directories and add to the count
            new_files_count += move_folder_contents(src_path, dst_path, extensions)
        else:
            # Check if the file is new before moving
            is_new_file = not os.path.exists(dst_path)
            
            # If extensions are specified, check the file extension
            if extensions:
                file_ext = os.path.splitext(item)[1].lower()
                if file_ext in extensions:
                    move_and_compare(src_path, dst_path)
                    if is_new_file:
                        new_files_count += 1
            else:
                # If no extensions are specified, move the file
                move_and_compare(src_path, dst_path)
                if is_new_file:
                    new_files_count += 1
    return new_files_count

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

def create_m3u8_playlist(playlist_folder_path, playlist_name, song_files):
    """
    Creates an .m3u8 playlist file in the destination folder.
    """
    m3u8_file_path = os.path.join(playlist_folder_path, f"{playlist_name}.m3u8")
    logging.info(f"Creating .m3u8 playlist file at: {m3u8_file_path}")
    try:
        with open(m3u8_file_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            # Sort files alphabetically for consistent playlist order
            for song_file in sorted(song_files):
                f.write(f"{song_file}\n")
        logging.info(f"✓ Successfully created playlist file: {m3u8_file_path}")
    except Exception as e:
        logging.error(f"Failed to create .m3u8 playlist file for '{playlist_name}': {e}")

def move_playlist_folders():
    music_extensions = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.wma', '.webm', '.opus', '.mka'}
    playlist_extensions = {'.m3u', '.m3u8'}

    download_path_clean = str(download_path).strip('"').strip("'").rstrip('/')
    logging.info(f"Cleaned download_path: {repr(download_path_clean)}")

    if not os.path.exists(download_path_clean):
        logging.error(f"Download path does not exist: {download_path_clean}")
        return

    files_in_download_path = os.listdir(download_path_clean)
    logging.info(f"Checking for playlist folders in: {download_path_clean}, found {len(files_in_download_path)} items")    

    total_files_moved = 0
    playlists_to_process = []

    # --- First Pass: Move files and collect data ---
    for item in files_in_download_path:
        item_path = os.path.join(download_path_clean, item)
        if not os.path.isdir(item_path):
            continue

        try:
            all_files = os.listdir(item_path)
            # Check if folder contains playlist files
            playlist_files = [f for f in all_files if os.path.splitext(f)[1].lower() in playlist_extensions]
            music_files = [f for f in all_files if os.path.splitext(f)[1].lower() in music_extensions]
        except Exception as e:
            logging.error(f"Error reading directory {item_path}: {e}")
            continue

        # Only process folders that contain playlist files
        if playlist_files and music_files:
            playlist_name = item
            destination_folder = os.path.join(playlist_dir, playlist_name)
            logging.info(f"Found playlist folder '{playlist_name}' with {len(music_files)} songs and {len(playlist_files)} playlist files.")

            try:
                if os.path.exists(destination_folder):
                    logging.warning(f"Playlist folder {destination_folder} already exists. Merging music files.")
                
                newly_moved_count = move_folder_contents(item_path, destination_folder, music_extensions)

                logging.info(f"Moved {newly_moved_count} new music files to: {destination_folder}")
                total_files_moved += newly_moved_count
                playlists_to_process.append((playlist_name, music_files, item_path, destination_folder))

            except Exception as e:
                logging.error(f"Error moving music files from {item_path}: {e}")
        elif playlist_files and not music_files:
            logging.info(f"Found playlist folder '{item}' with only playlist files, no music files. Skipping.")
        elif not playlist_files and music_files:
            logging.info(f"Found folder '{item}' with music files but no playlist files. Skipping.")

    # --- Second Pass: Create playlists if files were moved ---
    if total_files_moved > 0:
        logging.info(f"Moved a total of {total_files_moved} files. Processing playlists...")
        for playlist_name, music_files, original_item_path, destination_folder in playlists_to_process:
            # Create the .m3u8 file
            create_m3u8_playlist(destination_folder, playlist_name, music_files)

            if os.path.exists(original_item_path):
                try:
                    shutil.rmtree(original_item_path, ignore_errors=True)
                    logging.info(f"✓ Cleaned up leftover downloaded folder: {original_item_path}")
                except Exception as e:
                    logging.error(f"Failed to remove leftover folder {original_item_path}: {e}")
    else:
        logging.info("No new playlist files to move")
                    
# Main function to iterate through all artist folders in the source root
def main():
    logging.info(f"Starting the music sorting script with source_route: {source_route}")

    # Move playlist folders
    move_playlist_folders()

    # Delete specific files (.cue, .log, .m3u) in relevant directories before any other processing
    # delete_specific_files_in_all_subdirectories(source_route)
    # delete_specific_files_in_all_subdirectories(music_download_folder)
    # delete_specific_files_in_all_subdirectories(unknown_albums_dir)

    # Make sure the new artist directory exists
    # if not os.path.exists(new_artists_dir):
    #     os.makedirs(new_artists_dir)
    #     logging.info(f"Created new artists directory: {new_artists_dir}")
    # else:
    #     logging.info(f"New artists directory already exists: {new_artists_dir}")

    # # Log all items in source_route
    # items = os.listdir(source_route)
    # logging.info(f"Items in source_route: {items}")

    # # Process each artist folder in source_route
    # for item in items:
    #     artist_folder = os.path.join(source_route, item)
    #     logging.info(f"Processing item: {item}")

    #     if os.path.isdir(artist_folder):
    #         logging.info(f"Found artist folder: {artist_folder}, starting process_artist_folder")
    #         process_artist_folder(artist_folder)
    #     else:
    #         logging.info(f"Skipping non-directory item: {artist_folder}")

    # # After processing artist folders, move folders with audio files to unknown album folder
    # move_folders_with_audio_to_unknown()

    # # Run the empty directory cleanup after processing everything else at source
    # cleanup_empty_directories(source_route)

    # # Run the empty directory cleanup after processing everything else at download folder
    # cleanup_empty_directories(music_download_folder)
    
    # # Run the empty directory cleanup after processing everything else at unknown album folder
    # cleanup_empty_directories(unknown_albums_dir)

if __name__ == "__main__":
    main()