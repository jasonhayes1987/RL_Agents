import logging
import os

# Clear the debug.log file if it exists
log_file = 'debug.log'
if os.path.exists(log_file):
    with open(log_file, 'w'):
        pass

# Create a custom logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)  # Set the lowest level to capture all types of log messages

# Create handlers
console_handler = logging.StreamHandler()
file_handler = logging.FileHandler(log_file)

# Set levels for handlers
console_handler.setLevel(logging.ERROR)
file_handler.setLevel(logging.ERROR)

# Create formatters and add them to the handlers
console_format = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
file_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler.setFormatter(console_format)
file_handler.setFormatter(file_format)

# Clear any existing handlers
if logger.hasHandlers():
    logger.handlers.clear()

# Add handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)