import os
import subprocess
import logging
import sys

# Set up logging
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'RunAll.log')
logging.basicConfig(                                                                                                
    level=logging.INFO,                                                                                             
    format='%(asctime)s - %(levelname)s - %(message)s',                                                             
    handlers=[                                                                                                      
        logging.FileHandler(log_file),                                                                              
        logging.StreamHandler(sys.stdout)                                                                           
    ]                                                                                                               
)                                                                                                                   
# Get the current script's directory
current_dir = os.path.dirname(os.path.abspath(__file__))

# Path to the shell script 'UpdatewithMB.sh'
sh_script_path = os.path.join(current_dir, 'UpdatewithMB.sh')

# Path to the Python script 'Sort_MoveMusicDownloads.py'
python_script_path = os.path.join(current_dir, 'Sort_MoveMusicDownloads.py')

def run_script(command):
    """Runs a script and logs its output."""
    try:
        logging.info(f"Running command: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        if result.stdout:
            logging.info(f"--- STDOUT ---\n{result.stdout.strip()}")
        if result.stderr:
            logging.warning(f"--- STDERR ---\n{result.stderr.strip()}")
        logging.info(f"Command finished successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with exit code {e.returncode}.")
        if e.stdout:
            logging.error(f"--- STDOUT ---\n{e.stdout.strip()}")
        if e.stderr:
            logging.error(f"--- STDERR ---\n{e.stderr.strip()}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return False

# Run the shell script first
logging.info("--- Starting UpdatewithMB.sh ---")
run_script(['bash', sh_script_path])

# After the shell script finishes, run the Python script
logging.info("--- Starting Sort_MoveMusicDownloads.py ---")
run_script(['python3', python_script_path])

logging.info("--- All post-download scripts finished. ---")


