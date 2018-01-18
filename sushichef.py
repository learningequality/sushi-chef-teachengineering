#!/usr/bin/env python

from bs4 import BeautifulSoup
from bs4 import Tag
from collections import OrderedDict
import copy
from http import client
import json
from le_utils.constants import licenses, content_kinds, file_formats
import logging
import ntpath
import os
import pafy
from pathlib import Path
import re
import requests
from ricecooker.classes.licenses import get_license
from ricecooker.chefs import JsonTreeChef
from ricecooker.utils.caching import CacheForeverHeuristic, FileCache, CacheControlAdapter
from ricecooker.utils import downloader, html_writer
from ricecooker.utils.jsontrees import write_tree_to_json_tree, SUBTITLES_FILE
import sys
import time
from urllib.error import URLError
from urllib.parse import urljoin
import youtube_dl


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
TIME_SLEEP = .1

# webcache
###############################################################
sess = requests.Session()
#cache = FileCache('.webcache')
#basic_adapter = CacheControlAdapter(cache=cache)
#forever_adapter = CacheControlAdapter(heuristic=CacheForeverHeuristic(), cache=cache)
#sess.mount('http://', basic_adapter)
#sess.mount(BASE_URL, forever_adapter)

# Main Scraping Method
################################################################################

def test():
    """
    Test individual resources
    """
    url = "https://www.teachengineering.org/activities/view/gat_esr_test_activity1"
    #collection_type = "Sprinkles"
    #collection_type = "MakerChallenges"
    #collection_type = "Lessons"
    collection_type = "Activities"
    try:
        subtopic_name = "test"
        document = downloader.read(url, loadjs=False, session=sess)
        page = BeautifulSoup(document, 'html.parser')
        collection = Collection(page, filepath="/tmp/lesson-"+subtopic_name+".zip", 
            source_id=url,
            type=collection_type)
        collection.to_file(PATH, [collection_type.lower()])
    except requests.exceptions.HTTPError as e:
        LOGGER.info("Error: {}".format(e)) 


def check_subtitles(page):
    import csv
    c = CollectionSection(page)
    urls = c.get_videos_urls()
    for url in urls:
        video = YouTubeResource(url)
        info = video.get_video_info()
        if isinstance(info, dict) and len(info.keys()) > 0:
            with open("/tmp/subtitles.csv", 'a') as csv_file:
                csv_writer = csv.writer(csv_file, delimiter=",")
                csv_writer.writerow([url] + list(info["subtitles"].keys()))
            

class ResourceBrowser(object):
    def __init__(self, resource_url):
        self.resource_url = resource_url

    def get_resource_data(self):
        try:
            page_contents = downloader.read(self.resource_url, loadjs=True)
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

    def json_browser_url(self, azureSearchSettings, offset=0, batch=10):
        return "https://{serviceName}.search.windows.net/indexes/{indexName}/docs?api-version={apiVersion}&api-key={apiKey}&search=&%24count=true&%24top={batch}&%24skip={offset}&searchMode=all&scoringProfile=FieldBoost&%24orderby=sortableTitle".format(batch=batch, offset=offset, **azureSearchSettings)

    def run(self):
        settings = self.get_resource_data()
        offset = 0
        batch = 10
        while True:
            url = self.json_browser_url(settings, offset=offset, batch=batch)
            req = requests.get(url)
            data = req.json()
            try:
                #num_registers = data["@odata.count"]
                num_registers = 10
            except KeyError:
                LOGGER.info("The json object is bad formed: {}".format(data))
                LOGGER.info("retry...")
                time.sleep(3)
            else:
                queue = data["value"]
                LOGGER.info("CRAWLING : OFFSET {}".format(offset))
                while len(queue) > 0:
                    resource = queue.pop(0)
                    url = self.build_resource_url(resource["id"], resource["collection"])
                    if resource["spanishVersionId"] is not None:
                        url_es = self.build_resource_url(resource["spanishVersionId"], resource["collection"])
                    else:
                        url_es = None
                    yield dict(url=url, collection=resource["collection"],
                        url_es=url_es,
                        spanishVersionId=resource["spanishVersionId"],
                        title=resource["title"], summary=resource["summary"],
                        grade_target=resource["gradeTarget"],
                        grade_range=resource["gradeRange"],
                        id=resource["id"])
                offset += batch
                if offset > num_registers:
                    break

    def build_resource_url(self, id_name, collection):
        return urljoin(BASE_URL, collection.lower()+"/view/"+id_name)


class Menu(object):
    """
        This class checks elements on the lesson menu and build the menu list
    """
    def __init__(self, page, filepath=None, id_=None, exclude_titles=None, 
                include_titles=None):
        self.body = page.find("div", id=id_)
        self.menu = OrderedDict()
        self.filepath = filepath
        self.exclude_titles = [] if exclude_titles is None else exclude_titles
        self.license = None
        if include_titles is not None:
            for title_id, title_text in include_titles:
                self.add(title_id, title_text)
        self.menu_titles(self.body.find_all("li"))

    def write(self, content):
        with html_writer.HTMLWriter(self.filepath, "w") as zipper:
            zipper.write_index_contents(content)

    def to_file(self):
        self.write('<html><body><meta charset="UTF-8"></head><ul>'+self.to_html()+'</ul></body></html>')

    def menu_titles(self, titles):
        for title in titles:
            title_id = title.find("a")["href"].replace("#", "")
            self.add(title_id, title.text)

    def get(self, name):
        try:
            return self.menu[name]["filename"]
        except KeyError:
            return None

    def add(self, title_id, title):
        name = title.lower().strip().replace(" ", "_").replace("/", "_")
        if not title_id in self.exclude_titles:
            self.menu[title_id] = {
                "filename": "{}.html".format(name),
                "text": title,
                "section": None,
            }

    def remove(self, title_id):
        try:
            del self.menu[title_id]
        except KeyError:
            pass

    def set_section(self, section):
        if section.id is not None:
            self.menu[section.id]["section"] = section.menu_name
        return self.get(section.id)

    def to_html(self, directory="files/", active_li=None):
        li = []
        for e in self.menu.values():
            li.append("<li>")
            if active_li is not None and e["filename"] == active_li:
                li.append('{text}'.format(text=e["text"]))
            else:
                li.append('<a href="{directory}{filename}">{text}</a>'.format(directory=directory, **e))
            li.append("</li>")
        return "".join(li)

    def check(self):
        for name, values in self.menu.items():
            if values["section"] is None:
                raise Exception("{} is not linked to a section".format(name))

    def info(self):
        return dict(
            kind=content_kinds.HTML5,
            source_id=self.filepath,
            title="Menu Index",
            description="",
            license=self.license,
            language="en",
            files=[
                dict(
                    file_type=content_kinds.HTML5,
                    path=self.filepath
                )
            ]
        ) 


class CurriculumType(object):
     def render(self, page, menu_filename):
        for meta_section in self.sections:
            Section = meta_section["class"]
            if isinstance(Section, list):
                section = sum([subsection(page, filename=menu_filename, 
                                menu_name=meta_section["menu_name"])
                                for subsection in Section])
                section.id = meta_section["id"] 
            else:
                section = Section(page, filename=menu_filename, id_=meta_section["id"], 
                                menu_name=meta_section["menu_name"])
            yield section


#the ids are fixed by the web page
class Activity(CurriculumType):
    def __init__(self):
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "summary", "class": [CurriculumHeader, Summary, EngineeringConnection], 
            "menu_name": "summary"},
            {"id": "prereq", "class": CollectionSection, "menu_name": "pre-req_knowledge"},
            {"id": "objectives", "class": CollectionSection, "menu_name": "learning_objectives"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "mats", "class": CollectionSection, "menu_name": "materials_list"},
            {"id": "intro", "class": CollectionSection, "menu_name": "introduction_motivation"},
            {"id": "vocab", "class": CollectionSection, "menu_name": "vocabulary_definitions"},
            {"id": "procedure", "class": CollectionSection, "menu_name": "procedure"},
            {"id": "safety", "class": CollectionSection, "menu_name": "safety_issues"},
            {"id": "quest", "class": CollectionSection, "menu_name": "investigating_questions"},
            {"id": "troubleshooting", "class": CollectionSection, "menu_name": "troubleshooting_tips"},
            {"id": "assessment", "class": CollectionSection, "menu_name": "assessment"},
            {"id": "scaling", "class": CollectionSection, "menu_name": "activity_scaling"},
            {"id": "extensions", "class": CollectionSection, "menu_name": "activity_extensions"},
            {"id": "multimedia", "class": CollectionSection, "menu_name": "additional_multimedia_support"},
            {"id": "references", "class": CollectionSection, "menu_name": "references"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class Lesson(CurriculumType):
    def __init__(self):
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "summary", "class": [CurriculumHeader, Summary, EngineeringConnection], "menu_name": "summary"},
            {"id": "prereq", "class": CollectionSection, "menu_name": "pre-req_knowledge"},
            {"id": "objectives", "class": CollectionSection, "menu_name": "learning_objectives"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "intro", "class": CollectionSection, "menu_name": "introduction_motivation"},
            {"id": "background", "class": CollectionSection, "menu_name": "background"},
            {"id": "vocab", "class": CollectionSection, "menu_name": "vocabulary_definitions"},
            {"id": "assoc", "class": CollectionSection, "menu_name": "associated_activities"},
            {"id": "closure", "class": CollectionSection, "menu_name": "lesson_closure"},
            {"id": "assessment", "class": CollectionSection, "menu_name": "assessment"},
            {"id": "multimedia", "class": CollectionSection, "menu_name": "additional_multimedia_support"},
            {"id": "extensions", "class": CollectionSection, "menu_name": "extensions"},
            {"id": "references", "class": CollectionSection, "menu_name": "references"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class CurricularUnit(CurriculumType):
    def __init__(self):
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "summary", "class": [CurriculumHeader, Summary], "menu_name": "summary"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "overview", "class": CollectionSection, "menu_name": "unit_overview"},
            {"id": "schedule", "class": CollectionSection, "menu_name": "unit_schedule"},
            {"id": "assessment", "class": CollectionSection, "menu_name": "assessment"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class Sprinkle(CurriculumType):
    def __init__(self):
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "intro", "class": [CurriculumHeader, Introduction], "menu_name": "introduction"},
            {"id": "sups", "class": CollectionSection, "menu_name": "supplies"},
            {"id": "procedure", "class": CollectionSection, "menu_name": "procedure"},
            {"id": "wrapup", "class": CollectionSection, "menu_name": "wrap_up_-_thought_questions"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class MakerChallenge(CurriculumType):
    def __init__(self):
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "summary", "class": [CurriculumHeader, Summary], "menu_name": "maker_challenge_recap"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "mats", "class": CollectionSection, "menu_name": "maker_materials_&_supplies"},
            {"id": "kickoff", "class": CollectionSection, "menu_name": "kickoff"},
            {"id": "resources", "class": CollectionSection, "menu_name": "resources"},
            {"id": "makertime", "class": CollectionSection, "menu_name": "maker_time"},
            {"id": "wrapup", "class": CollectionSection, "menu_name": "wrap_up"},
            {"id": "tips", "class": CollectionSection, "menu_name": "tips"},
            {"id": "other", "class": CollectionSection, "menu_name": "other"},
            {"id": "acknowledgements", "class": CollectionSection, "menu_name": "acknowledgements"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram],
            "menu_name": "info"},
        ]


class Collection(object):
    def __init__(self, url, source_id, type, title):
        self.page = self.download_page(url)
        if self.page is not False:
            self.title_prefix = self.clean_title(self.page.find("span", class_="title-prefix"))
            self.title = self.clean_title(self.page.find("span", class_="curriculum-title"))
            if self.title is None:
                self.title = title
            self.contribution_by = None
            filepath = build_path(['chefdata', 'menu'])
            self.filepath = "{path}/{source_id}.zip".format(path=filepath, 
                source_id=source_id)
            self.menu = Menu(self.page, filepath=self.filepath, id_="CurriculumNav", 
                exclude_titles=["attachments", "comments"], 
                include_titles=[("quick", "Quick Look")])
            self.menu.add("info", "Info")
            self.source_id = source_id
            self.resource_url = url
            self.type = type
            self.license = None
            
            if type == "MakerChallenges":
                self.curriculum_type = MakerChallenge()
            elif type == "Lessons":
                self.curriculum_type = Lesson()
            elif type == "Activities":
                self.curriculum_type = Activity()
            elif type == "CurricularUnits":
                self.curriculum_type = CurricularUnit()
            elif type == "Sprinkles":
                self.curriculum_type = Sprinkle()

    def download_page(self, url):
        tries = 0
        while tries < 4:
            try:
                document = downloader.read(url, loadjs=False)#, session=sess)
            except requests.exceptions.HTTPError as e:
                LOGGER.info("Error: {}".format(e))
            except requests.exceptions.ConnectionError:
                ### this is a weird error, may be it's raised when teachengineering's webpage
                ### is slow to respond requested resources
                LOGGER.info("Connection error, the resource will be scraped in 5s...")
                time.sleep(3)
            else:
                return BeautifulSoup(document, 'html.parser')
            tries += 1
        return False


    def description(self):
        descr = self.page.find("meta", property="og:description")
        return descr.get("content", "")

    def clean_title(self, title):
        if title is not None:
            text = title.text.replace("\t", " ")
            return text.strip()

    def drop_null_sections(self):
        sections = []
        for section in self.curriculum_type.render(self.page, self.menu.filepath):
            if section.body is None:
                self.menu.remove(section.id)
            else:
                sections.append(section)
        return sections

    def to_file(self, channel_tree):
        LOGGER.info(" + [{}]: {}".format(self.type, self.title))
        LOGGER.info("   - URL: {}".format(self.resource_url))
        copy_page = copy.copy(self.page)
        cr = Copyright(copy_page)
        self.license = get_license(licenses.CC_BY, copyright_holder=cr.get_copyright_info()).as_dict()
        topic_node = dict(
            kind=content_kinds.TOPIC,
            source_id=self.resource_url,
            title=self.title,
            description=self.description(),
            license=self.license,
            children=[]
        )

        sections = self.drop_null_sections()
        #build the menu index
        self.menu.to_file()
        #set section's html files to the menu
        for section in sections:
            menu_filename = self.menu.set_section(section)
            menu_index = self.menu.to_html(directory="", active_li=menu_filename)
            section.to_file(menu_filename, menu_index=menu_index)

        self.menu.check()
        self.menu.license = self.license
        topic_node["children"].append(self.menu.info())
        #check for pdfs and videos on all page
        all_sections = CollectionSection(copy_page, resource_url=self.resource_url)
        pdfs_info = all_sections.build_pdfs_info(self.source_id, self.license)
        if pdfs_info is not None:
            topic_node["children"].append(pdfs_info)
        videos_info = all_sections.build_videos_info(self.source_id, self.license)
        if videos_info is not None:
            topic_node["children"].append(videos_info)
        channel_tree["children"].append(topic_node)

    #def rm(self, filepath):
    #    os.remove(filepath)


class CollectionSection(object):
    def __init__(self,  page, filename=None, id_=None, menu_name=None, resource_url=None):
        LOGGER.debug(id_)
        self.id = id_
        if id_ is None:
            self.body = page
        else:
            self.body = page.find("section", id=id_)

        if self.body is not None:
            h3 = self.body.find("h3")
            self.title = self.clean_title(h3)
            del h3
        else:
            self.title = None
        self.filename = filename
        self.menu_name = menu_name
        self.resource_url = resource_url

    def __add__(self, o):
        if isinstance(self.body, Tag) and isinstance(o.body, Tag):
            parent = Tag(name="div")
            parent.insert(0, self.body)
            parent.insert(1, o.body)
            self.body = parent
        elif self.body is None and isinstance(o.body, Tag):
            self.body = o.body
        else:
            LOGGER.info("Null sections: {} and {}".format(
                self.__class__.__name__, o.__class__.__name__))

        return self

    def __radd__(self, o):
        return self

    def clean_title(self, title):
        if title is not None:
            title = str(title)
        return title

    def get_content(self):
        content = self.body
        self.get_imgs()
        remove_links(content)
        return "".join([str(p) for p in content])

    def get_pdfs(self):
        urls = {}
        if self.body is not None:
            resource_links = self.body.find_all("a", href=re.compile("^\/content|https\:\/\/www.teachengineering"))
            for link in resource_links:
                if link["href"].endswith(".pdf") and link["href"] not in urls:
                    name = get_name_from_url(link["href"])
                    urls[link["href"]] = (name, urljoin(BASE_URL, link["href"]))
            return urls.values()

    def build_pdfs_info(self, source_id, license=None):
        pdfs_urls = self.get_pdfs()
        if len(pdfs_urls) == 0:
            return

        PDFS_DATA_DIR = build_path(["chefdata", 'pdfs', source_id])
        info = dict(
            kind=content_kinds.TOPIC,
            source_id="sourceid:"+PDFS_DATA_DIR,
            title="Files",
            description='',
            children=[],
            language="en",
            license=license)

        for name, pdf_url in pdfs_urls:
            try:
                response = downloader.read(pdf_url)
                pdf_filepath = os.path.join(PDFS_DATA_DIR, name)
                with open(pdf_filepath, 'wb') as f:
                    f.write(response)
                files = dict(
                    kind=content_kinds.DOCUMENT,
                    source_id=pdf_url,
                    title=get_name_from_url_no_ext(name),
                    description='',
                    files=[dict(
                        file_type=content_kinds.DOCUMENT,
                        path=pdf_filepath
                    )],
                    language="en",
                    license=license)
                info["children"].append(files)
            except requests.exceptions.HTTPError as e:
                LOGGER.info("Error: {}".format(e))

        return info

    def get_imgs(self):
        for img in self.body.find_all("img"):
            if img["src"].startswith("/"):
                img_src = urljoin(BASE_URL, img["src"])
            else:
                img_src = img["src"]
            filename = get_name_from_url(img_src)
            self.write_img(img_src, filename)
            img["src"] = filename

    def get_videos_urls(self):
        urls = set([])
        for iframe in self.body.find_all("iframe"):
            url = iframe["src"]
            if YouTubeResource.is_youtube(url):
                urls.add(YouTubeResource.transform_embed(url))
            iframe.extract()

        queue = self.body.find_all("a", href=re.compile("^http"))
        max_tries = 3
        num_tries = 0
        while queue:
            try:
                a = queue.pop(0)
                ### some links who are youtube resources have shorted thier ulrs
                ### with session.head we can expand it
                if check_shorter_url(a["href"]):
                    resp = sess.head(a["href"], allow_redirects=True)
                    url = resp.url
                else:
                    url = a["href"]
                if YouTubeResource.is_youtube(url, get_channel=False):
                    urls.add(url.strip())
            except requests.exceptions.MissingSchema:
                pass
            except requests.exceptions.TooManyRedirects:
                LOGGER.info("Too many redirections, skip resource: {}".format(a["href"]))
            except requests.exceptions.ConnectionError:
                ### this is a weird error, perhaps it's raised when teachengineering's webpage
                ### is slow to respond requested resources
                LOGGER.info(a["href"])
                num_tries += 1
                LOGGER.info("Connection error, the resource will be scraped in 3s... num try {}".format(num_tries))
                if num_tries < max_tries:
                    queue.insert(0, a)
                else:
                    LOGGER.info("Connection error, give up.")
                time.sleep(3)
            except KeyError:
                pass
            else:
                num_tries = 0
        return urls

    def build_videos_info(self, source_id, license=None):
        videos_urls = self.get_videos_urls()
        if len(videos_urls) == 0:
            return

        VIDEOS_DATA_DIR = build_path(["chefdata", 'videos', source_id])
        info = dict(
            kind=content_kinds.TOPIC,
            source_id="sourceid:"+VIDEOS_DATA_DIR,
            title="Videos",
            description='',
            children=[],
            language="en",
            license=license)

        for i, url in enumerate(videos_urls):
            resource = YouTubeResource(url)
            resource.to_file(filepath=VIDEOS_DATA_DIR)
            if resource.resource_file is not None:
                info["children"].append(resource.resource_file)
        return info

    def write(self, filename, content):
        with html_writer.HTMLWriter(self.filename, "a") as zipper:
            zipper.write_contents(filename, content, directory="files")

    def write_img(self, url, filename):
        with html_writer.HTMLWriter(self.filename, "a") as zipper:
            zipper.write_url(url, filename, directory="files")

    def to_file(self, filename, menu_index=None):
        if self.body is not None and filename is not None:
            content = self.get_content()

            if menu_index is not None:
                html = '<html><head><meta charset="UTF-8"></head><body>{}{}</body></html>'.format(
                    menu_index, content)
            else:
                html = '<html><head><meta charset="UTF-8"></head><body>{}</body></html>'.format(
                    content)

            self.write(filename, html)


class CurriculumHeader(CollectionSection):
    def __init__(self, page, filename=None, id_="curriculum-header", menu_name="summary"):
        self.body = page.find("div", class_="curriculum-header")
        self.filename = filename
        self.menu_name = menu_name
        self.id = id_
        #self.resources = []


class QuickLook(CollectionSection):
     def __init__(self, page, filename=None, id_="quick", menu_name="quick_look"):
        super(QuickLook, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find("div", class_="quick-look")
        ## cleaning html code
        for s in self.body.find_all("script"):
            s.extract()
        for b in self.body.find_all("button"):
            b.extract()
        div = self.body.find("div", id="PrintShareModal")
        div.extract()


class EngineeringConnection(CollectionSection):
    def __init__(self, page, filename=None, id_="engineering_connection", 
                menu_name="engineering_connection"):
        super(EngineeringConnection, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Engineering Connection\s*")))


class Summary(CollectionSection):
    def __init__(self, page, filename=None, id_="summary", menu_name="summary"):
        super(Summary, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)


class Introduction(CollectionSection):
    def __init__(self, page, filename=None, id_="intro", menu_name="introduction"):
        super(Introduction, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)


class Attachments(CollectionSection):
    def __init__(self, page, filename=None, id_="attachments", menu_name="attachments"):
        super(Attachments, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)


class Contributors(CollectionSection):
    def __init__(self, page, filename=None, id_="contributors", menu_name="contributors"):
        super(Contributors, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Contributors\s*")))


class SupportingProgram(CollectionSection):
    def __init__(self, page, filename=None, id_="supporting_program", menu_name="supporting_program"):
        super(SupportingProgram, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Supporting Program\s*")))


class Acknowledgements(CollectionSection):
    def __init__(self, page, filename=None, id_="acknowledgements", menu_name="acknowledgements"):
        super(Acknowledgements, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Acknowledgements\s*")))


class Copyright(CollectionSection):
    def __init__(self, page, filename=None, id_="copyright", menu_name="copyright"):
        super(Copyright, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Copyright\s*")))

    def get_copyright_info(self):
        text = self.body.text
        index = text.find("©")
        if index != -1:
            copyright = text[index:].strip()
            LOGGER.info("   - COPYRIGHT INFO:" + copyright)
        else:
            copyright = ""
        return copyright


class ResourceType(object):
    """
        Base class for File, WebPage, Video, Audio resources
    """
    def __init__(self, type_name=None):
        LOGGER.info("Resource Type: "+type_name)
        self.type_name = type_name
        self.resource_file = None

    def to_file(self, filepath=None):
        pass

    def add_resource_file(self, info):
        self.resource_file = info


class YouTubeResource(ResourceType):
    def __init__(self, resource_url, type_name="Youtube"):
        super(YouTubeResource, self).__init__(type_name=type_name)
        self.resource_url = self.clean_url(resource_url)
        self.file_format = file_formats.MP4

    def clean_url(self, url):
        if url[-1] == "/":
            url = url[:-1]
        return url.strip()

    @classmethod
    def is_youtube(self, url, get_channel=False):
        youtube = url.find("youtube") != -1 or url.find("youtu.be") != -1
        if get_channel is False:
            youtube = youtube and url.find("user") == -1 and url.find("/c/") == -1
        return youtube

    @classmethod
    def transform_embed(self, url):
        url = "".join(url.split("?")[:1])
        return url.replace("embed/", "watch?v=").strip()

    def get_video_info(self):
        ydl_options = {
                'writesubtitles': True,
                'allsubtitles': True,
                'no_warnings': True,
                'restrictfilenames':True,
                'continuedl': True,
                'quiet': False,
                'format': "bestvideo[height<={maxheight}][ext=mp4]+bestaudio[ext=m4a]/best[height<={maxheight}][ext=mp4]".format(maxheight='720')
            }

        with youtube_dl.YoutubeDL(ydl_options) as ydl:
            try:
                ydl.add_default_info_extractors()
                info = ydl.extract_info(self.resource_url, download=False)
                return info
            except(youtube_dl.utils.DownloadError, youtube_dl.utils.ContentTooShortError,
                    youtube_dl.utils.ExtractorError) as e:
                LOGGER.info('An error occured ' + str(e))
                LOGGER.info(self.resource_url)
            except KeyError as e:
                LOGGER.info(str(e))

    def subtitles_dict(self):
        video_info = self.get_video_info()
        video_id = video_info["id"]
        subtitles_info = video_info["subtitles"]
        subs = []
        for language in subtitles_info.keys():
            subs.append(dict(file_type=SUBTITLES_FILE, youtube_id=video_id, language=language))
        return subs

    def process_file(self, download=False, filepath=None):
        if download is True:
            video_filepath = self.video_download(download_to=filepath)
        else:
            video_filepath = None

        if video_filepath is not None:
            files = [dict(file_type=content_kinds.VIDEO, path=video_filepath)]
            files += self.subtitles_dict()

            self.add_resource_file(dict(
                kind=content_kinds.VIDEO,
                source_id=self.resource_url,
                title=get_name_from_url_no_ext(video_filepath),
                description='',
                files=files,
                language="en",
                license=get_license(licenses.CC_BY, copyright_holder="TeachEngineering").as_dict()))

    #youtubedl has some troubles downloading videos in youtube,
    #sometimes raises connection error
    #for that I choose pafy for downloading
    def video_download(self, download_to="/tmp/"):
        for try_number in range(10):
            try:
                video = pafy.new(self.resource_url)
                best = video.getbest(preftype="mp4")
                video_filepath = best.download(filepath=download_to)
            except (URLError, ConnectionResetError) as e:
                LOGGER.info(e)
                LOGGER.info("Download retry:"+str(try_number))
                time.sleep(.8)
            except (youtube_dl.utils.DownloadError, youtube_dl.utils.ContentTooShortError,
                    youtube_dl.utils.ExtractorError, OSError) as e:
                LOGGER.info("An error ocurred, may be the video is not available.")
                return
            else:
                return video_filepath

    def to_file(self, filepath=None):
        self.process_file(download=DOWNLOAD_VIDEOS, filepath=filepath)


def if_file_exists(filepath):
    file_ = Path(filepath)
    return file_.is_file()


def if_dir_exists(filepath):
    file_ = Path(filepath)
    return file_.is_dir()


def get_name_from_url(url):
    head, tail = ntpath.split(url)
    return tail or ntpath.basename(url)


def get_name_from_url_no_ext(url):
    path = get_name_from_url(url)
    path_split = path.split(".")
    if len(path_split) > 1:
        name = ".".join(path_split[:-1])
    else:
        name = path_split[0]
    return name


def remove_links(content):
    if content is not None:
        for link in content.find_all("a"):
            link.replaceWithChildren()


def check_shorter_url(url):
    shorters_urls = set(["bitly.com", "goo.gl", "tinyurl.com", "ow.ly", "ls.gd", 
                "buff.ly", "adf.ly", "bit.do", "mcaf.ee"])
    index_init = url.find("://")
    index_end = url[index_init+3:].find("/")
    if index_init != -1:
        if index_end == -1:
            index_end = len(url[index_init+3:])
        domain = url[index_init+3:index_end+index_init+3]
        check = len(domain) < 12 or domain in shorters_urls
        return check


def build_path(levels):
    path = os.path.join(*levels)
    if not if_dir_exists(path):
        os.makedirs(path)
    return path


class TeachEngineeringChef(JsonTreeChef):
    ROOT_URL = "https://{HOSTNAME}"
    HOSTNAME = "teachengineering.org"
    DATA_DIR = "chefdata"
    TREES_DATA_DIR = os.path.join(DATA_DIR, 'trees')
    CRAWLING_STAGE_OUTPUT = 'web_resource_tree.json'
    SCRAPING_STAGE_OUTPUT = 'ricecooker_json_tree.json'
    LICENSE = get_license(licenses.CC_BY, copyright_holder="TeachEngineering").as_dict()

    def __init__(self):
        build_path([TeachEngineeringChef.TREES_DATA_DIR])
        super(TeachEngineeringChef, self).__init__()

    def pre_run(self, args, options):
        self.crawl(args, options)
        self.scrape(args, options)

    def crawl(self, args, options):
        web_resource_tree = dict(
            kind='TeachEngineeringResourceTree',
            title='TeachEngineering',
            children=[]
        )
        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR,                     
                                    TeachEngineeringChef.CRAWLING_STAGE_OUTPUT)
        curriculum_url = urljoin(TeachEngineeringChef.ROOT_URL.format(HOSTNAME=TeachEngineeringChef.HOSTNAME), "curriculum/browse")
        resource_browser = ResourceBrowser(curriculum_url)
        for data in resource_browser.run():
            web_resource_tree["children"].append(data)
        with open(crawling_stage, 'w') as f:
            json.dump(web_resource_tree, f, indent=2)
        return web_resource_tree

    def scrape(self, args, options):
        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.CRAWLING_STAGE_OUTPUT)
        with open(crawling_stage, 'r') as f:
            web_resource_tree = json.load(f)
            assert web_resource_tree['kind'] == 'TeachEngineeringResourceTree'

        channel_tree = self._build_scraping_json_tree(web_resource_tree)
        scrape_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.SCRAPING_STAGE_OUTPUT)
        write_tree_to_json_tree(scrape_stage, channel_tree)
 
    def _build_scraping_json_tree(self, web_resource_tree):
        channel_tree = dict(
            source_domain=TeachEngineeringChef.HOSTNAME,
            source_id='teachengineering',
            title='TeachEngineering',
            description="""The TeachEngineering digital library is a collaborative project between faculty, students and teachers associated with five founding partner universities, with National Science Foundation funding. The collection continues to grow and evolve with new additions submitted from more than 50 additional contributor organizations, a cadre of volunteer teacher and engineer reviewers, and feedback from teachers who use the curricula in their classrooms."""[:400], #400 UPPER LIMIT characters allowed 
            thumbnail='https://www.teachengineering.org/images/logos/v-636511398960000000/TELogoNew.png',
            language='en',
            children=[],
            license=TeachEngineeringChef.LICENSE,
        )
        for resource in web_resource_tree["children"]:
            collection = Collection(resource["url"],
                            source_id=resource["id"],
                            type=resource["collection"],
                            title=resource["title"])
            collection.to_file(channel_tree)
        return channel_tree


class TeachEngineeringEsChef(JsonTreeChef):
    ROOT_URL = "https://{HOSTNAME}"
    HOSTNAME = "teachengineering.org"
    DATA_DIR = "chefdata"
    TREES_DATA_DIR = os.path.join(DATA_DIR, 'trees')
    CRAWLING_STAGE_OUTPUT = 'web_resource_tree.json'
    SCRAPING_STAGE_OUTPUT = 'ricecooker_json_tree.json'
    LICENSE = get_license(licenses.CC_BY, copyright_holder="TeachEngineering").as_dict()

    def __init__(self):
        build_path([TeachEngineeringChef.TREES_DATA_DIR])
        super(TeachEngineeringEsChef, self).__init__()

    def pre_run(self, args, options):
        self.crawl(args, options)
        self.scrape(args, options)

    def crawl(self, args, options):
        web_resource_tree = dict(
            kind='TeachEngineeringResourceTree',
            title='TeachEngineering (Spanish)',
            children=[]
        )
        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR,                     
                                    TeachEngineeringChef.CRAWLING_STAGE_OUTPUT)
        curriculum_url = urljoin(TeachEngineeringChef.ROOT_URL.format(HOSTNAME=TeachEngineeringChef.HOSTNAME), "curriculum/browse")
        resource_browser = ResourceBrowser(curriculum_url)
        for data in resource_browser.run():
            if data["spanishVersionId"] is not None:
                web_resource_tree["children"].append(data)
        with open(crawling_stage, 'w') as f:
            json.dump(web_resource_tree, f, indent=2)
        return web_resource_tree

    def scrape(self, args, options):
        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.CRAWLING_STAGE_OUTPUT)
        with open(crawling_stage, 'r') as f:
            web_resource_tree = json.load(f)
            assert web_resource_tree['kind'] == 'TeachEngineeringResourceTree'

        channel_tree = self._build_scraping_json_tree(web_resource_tree)
        scrape_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.SCRAPING_STAGE_OUTPUT)
        write_tree_to_json_tree(scrape_stage, channel_tree)

    def _build_scraping_json_tree(self, web_resource_tree):
        channel_tree = dict(
            source_domain=TeachEngineeringChef.HOSTNAME,
            source_id='teachengineering',
            title='TeachEngineering (Spanish)',
            description="""The TeachEngineering digital library is a collaborative project between faculty, students and teachers associated with five founding partner universities, with National Science Foundation funding. The collection continues to grow and evolve with new additions submitted from more than 50 additional contributor organizations, a cadre of volunteer teacher and engineer reviewers, and feedback from teachers who use the curricula in their classrooms."""[:400], #400 UPPER LIMIT characters allowed 
            thumbnail='https://www.teachengineering.org/images/logos/v-636511398960000000/TELogoNew.png',
            language='es',
            children=[],
            license=TeachEngineeringChef.LICENSE,
        )
        for resource in web_resource_tree["children"]:
            if resource["spanishVersionId"] is not None:
                collection = Collection(resource["url_es"],
                        source_id=resource["spanishVersionId"],
                        type=resource["collection"],
                        title=resource["title"])
                collection.to_file(channel_tree)
        return channel_tree


# CLI: This code will run when `souschef.py` is called on the command line
################################################################################
if __name__ == '__main__':
    chef = TeachEngineeringChef()
    chef.main()
