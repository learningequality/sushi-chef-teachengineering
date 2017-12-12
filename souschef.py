#!/usr/bin/env python
import os
import sys
from ricecooker.utils import data_writer, path_builder, downloader, html_writer
from le_utils.constants import licenses, exercises, content_kinds, file_formats, format_presets, languages

from collections import OrderedDict
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from le_utils.constants import licenses, file_formats
import json
import requests
from ricecooker.classes.files import download_from_web, config
from ricecooker.utils.caching import CacheForeverHeuristic, FileCache, CacheControlAdapter


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
LOGGER = logging.getLogger()
__logging_handler = logging.StreamHandler()
LOGGER.addHandler(__logging_handler)
LOGGER.setLevel(logging.INFO)

# BASE_URL is used to identify when a resource is owned by Edsitement
BASE_URL = "https://www.teachengineering.org"

# If False then no download is made
# for debugging proporses
DOWNLOAD_VIDEOS = False

# time.sleep for debugging proporses, it helps to check log messages
TIME_SLEEP = .2


# Main Scraping Method
################################################################################
def scrape_source(writer):
    """
    Scrapes channel page and writes to a DataWriter
    Args: writer (DataWriter): class that writes data to folder/spreadsheet structure
    Returns: None
    """
    CURRICULUM_BROWSE_URL = urljoin(BASE_URL, "curriculum/browse")
    LOGGER.info("Checking data from: " + CURRICULUM_BROWSE_URL)
    #resource_browser = ResourceBrowser(CURRICULUM_BROWSE_URL)
    #resource_browser.run()
    collection = Collection("https://www.teachengineering.org/activities/view/cub_human_lesson06_activity1")
    collection.parse()


class ResourceBrowser(object):
    def __init__(self, resource_url):
        self.resource_url = resource_url

    def get_resource_data(self):
        try:
            page_contents = downloader.read(self.resource_url, loadjs=True)#, session=sess)
        except requests.exceptions.HTTPError as e:
            LOGGER.info("Error: {}".format(e))
        page = BeautifulSoup(page_contents, 'html.parser')
        scripts = page.find_all("script")
        keys = ["serviceName", "indexName", "apiKey", "apiVersion"]
        azureSearchSettings = {}
        for scriptjs in scripts:
            textValue = scriptjs.text
            try:
                for elem in textValue.split('{', 1)[1].rsplit('}', 1):
                    for kv in elem.split(","):
                        try:
                            k, v = kv.split(":")
                            k = k.strip().replace('"', "").replace("'", "")
                            v = v.strip().replace('"', "").replace("'", "")
                            if k in keys:
                                azureSearchSettings[k] = v 
                        except ValueError:
                            pass
            except IndexError:
                pass
        return azureSearchSettings

    def build_resource_url(self, azureSearchSettings, offset=0):
        return "https://{serviceName}.search.windows.net/indexes/{indexName}/docs?api-version={apiVersion}&api-key={apiKey}&search=&%24count=true&%24top=10&%24skip={offset}&searchMode=all&scoringProfile=FieldBoost&%24orderby=sortableTitle".format(offset=offset, **azureSearchSettings)

    def run(self):
        settings = self.get_resource_data()
        offset = 0
        while True:
            url = self.build_resource_url(settings, offset=offset)
            req = requests.get(url)
            data = req.json()
            #num_registers = data["@odata.count"]
            for resource in data["value"]:
                url = self.build_resource_url(resource["id"], resource["collection"])
                collection = Collection(url)
                time.sleep(1)
                break
            return

    def build_resource_url(id_name, collection):
        return urljoin(BASE_URL, collection.lower()+"/view/"+id_name)


class Collection(object):
    def __init__(self, resource_url):
        self.title = None
        self.title_prefix = None
        self.contribution_by = None
        self.resource_url = resource_url
        self.sections = [
            Summary,
            EngineeringConnection,
            LearningObjetives,
            MoreLikeThis
        ]

    def parse(self):
        try:
            page_contents = downloader.read(self.resource_url, loadjs=False)#, session=sess)
            page = BeautifulSoup(page_contents, 'html.parser')
            title_prefix = page.find("span", class_="title-prefix")
            title = page.find("span", class_="curriculum-title")
            
            for Section in self.sections:
                section = Section(page)
                print(section.get_content())

        except requests.exceptions.HTTPError as e:
            LOGGER.info("Error: {}".format(e))


class CollectionSection(object):
    def __init__(self,  page, filename=None, id_=None, menu_name=None):
        LOGGER.debug(id_)
        self.body = page.find("section", id=id_)
        if self.body is not None:
            h3 = self.body.find("h3")
            self.title = self.clean_title(h3)
            del h3
        self.filename = filename
        self.menu_name = menu_name

    def clean_title(self, title):
        if title is not None:
            title = str(title)
        return title

    def get_content(self):
        content = self.body
        #remove_links(content)
        return "".join([str(p) for p in content])


class Summary(CollectionSection):
    def __init__(self, page, filename=None, id_=None, menu_name=None):
        super(Summary, self).__init__(page, filename=filename,
                id_="summary", menu_name="")


class EngineeringConnection(CollectionSection):
    def __init__(self, page, filename=None, id_=None, menu_name=None):
        self.body = page.find_all(lambda tag: tag.name=="section" and tag.findChildren("h3", class_="text-highlight"))
        
    def get_content(self):
        for s in self.body:
            h3 = s.find("h3", class_="text-highlight")
            if h3.text.strip() == "Engineering Connection":
                self.title = h3.text.strip()
                print("OK", s.find_all("p"))
                break


class LearningObjetives(CollectionSection):
    def __init__(self, page, filename=None, id_=None, menu_name=None):
        super(LearningObjetives, self).__init__(page, filename=filename,
                id_="objectives", menu_name="")


class MoreLikeThis(CollectionSection):
    def __init__(self, page, filename=None, id_=None, menu_name=None):
        super(MoreLikeThis, self).__init__(page, filename=filename,
                id_="morelikethis", menu_name="")


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
