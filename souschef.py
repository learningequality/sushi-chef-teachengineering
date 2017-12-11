#!/usr/bin/env python
import os
import sys
from ricecooker.utils import data_writer, path_builder, downloader, html_writer
from le_utils.constants import licenses, exercises, content_kinds, file_formats, format_presets, languages


# Channel constants
################################################################################
CHANNEL_NAME = "Teach Engineering"              # Name of channel
CHANNEL_SOURCE_ID = "teachengineering-en"    # Channel's unique id
CHANNEL_DOMAIN = "teachengineering.org"          # Who is providing the content
CHANNEL_LANGUAGE = "en"      # Language of channel
CHANNEL_DESCRIPTION = None                                  # Description of the channel (optional)
CHANNEL_THUMBNAIL = None                                    # Local path or url to image file (optional)
PATH = path_builder.PathBuilder(channel_name=CHANNEL_NAME)  # Keeps track of path to write to csv
WRITE_TO_PATH = "{}{}{}.zip".format(os.path.dirname(os.path.realpath(__file__)), os.path.sep, CHANNEL_NAME) # Where to generate zip file

# Additional Constants
################################################################################




# Helper Methods
################################################################################
def get_text(element):
    """
    Extract text contents of the Beautiful Soup element `element`, normalizing
    newlines to spaces and stripping whitespace.
    """
    if element is None:
        return ''
    else:
        return element.get_text().replace('\r', '').replace('\n', ' ').strip()




# Main Scraping Method
################################################################################
def scrape_source(writer):
    """
    Scrapes channel page and writes to a DataWriter
    Args: writer (DataWriter): class that writes data to folder/spreadsheet structure
    Returns: None
    """
    # TODO: Put your ETL code here: crawling, scraping, content transformations, file and metadata writing
    raise NotImplementedError("Scraping method not implemented")




# CLI: This code will run when `souschef.py` is called on the command line
################################################################################
if __name__ == '__main__':
    # Open a writer to generate files
    with data_writer.DataWriter(write_to_path=WRITE_TO_PATH) as writer:
        # Write channel details to spreadsheet
        thumbnail = writer.add_file(str(PATH), "Channel Thumbnail", CHANNEL_THUMBNAIL, write_data=False)
        writer.add_channel(CHANNEL_NAME, CHANNEL_SOURCE_ID, CHANNEL_DOMAIN, CHANNEL_LANGUAGE, description=CHANNEL_DESCRIPTION, thumbnail=thumbnail)
        # Scrape source content
        scrape_source(writer)
        sys.stdout.write("\n\nDONE: Zip created at {}\n".format(writer.write_to_path))
