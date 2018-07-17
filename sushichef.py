#!/usr/bin/env python

from bs4 import BeautifulSoup
from bs4 import Tag
from collections import OrderedDict, defaultdict
import copy
from http import client
import gettext
import json
from le_utils.constants import licenses, content_kinds, file_formats
import logging
import ntpath
import os
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
DOWNLOAD_VIDEOS = True

# time.sleep for debugging proporses, it helps to check log messages
TIME_SLEEP = .1

DATA_DIR = "chefdata"
GENERAL_COPYRIGHT_HOLDER = "TeachEngineering digital library  © 2013 by Regents of the University of Colorado; original © 2013 Board of Regents, University of Nebraska"
#Curricular units with its lessons
CURRICULAR_UNITS_MAP = defaultdict(OrderedDict)
#Lessons related with curricular units
LESSONS_CURRICULAR_MAP = defaultdict(set)
# webcache
###############################################################
sess = requests.Session()
cache = FileCache('.webcache')
basic_adapter = CacheControlAdapter(cache=cache)
forever_adapter = CacheControlAdapter(heuristic=CacheForeverHeuristic(), cache=cache)
sess.mount('http://', basic_adapter)
sess.mount(BASE_URL, forever_adapter)

# Main Scraping Method
################################################################################

def test():
    """
    Test individual resources
    """
    #url = "https://www.teachengineering.org/activities/view/gat_esr_test_activity1"
    #url = "https://www.teachengineering.org/curricularunits/view/cub_dams"
    #url = "https://www.teachengineering.org/curricularunits/view/umo_sensorswork_unit"
    url = "https://www.teachengineering.org/curricularunits/view/cub_service_unit"
    #collection_type = "Sprinkles"
    #collection_type = "MakerChallenges"
    #collection_type = "Lessons"
    #collection_type = "Activities"
    collection_type = "CurricularUnits"
    channel_tree = dict(
        source_domain="teachengineering.org",
        source_id='teachengineering',
        title='TeachEngineering',
        description="""The TeachEngineering digital library is a collaborative project between faculty, students and teachers associated with five founding partner universities, with National Science Foundation funding. The collection continues to grow and evolve with new additions submitted from more than 50 additional contributor organizations, a cadre of volunteer teacher and engineer reviewers, and feedback from teachers who use the curricula in their classrooms."""[:400], #400 UPPER LIMIT characters allowed 
        thumbnail="",
        language="en",
        children=[],
        license=get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict(),
    )

    try:
        subtopic_name = "test"
        collection = Collection(url, 
            title="test",
            source_id="test",
            type=collection_type,
            lang="en")
        collection.to_file(channel_tree)
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
                num_registers = data["@odata.count"]
                #num_registers = 4
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
                include_titles=None, lang="en"):
        if page:
            self.body = page.find("div", id=id_)
        else:
            self.body = None
        self.menu = OrderedDict()
        self.filepath = filepath
        self.exclude_titles = [] if exclude_titles is None else exclude_titles
        self.license = None
        self.lang = lang
        if include_titles is not None:
            for title_id, title_text in include_titles:
                self.add(title_id, title_text)
        if self.body:
            self.menu_titles(self.body.find_all("li"))

    def write(self, content):
        with html_writer.HTMLWriter(self.filepath, "w") as zipper:
            zipper.write_index_contents(content)

    def write_css_js(self, filepath):
        with html_writer.HTMLWriter(filepath, "a") as zipper, open("chefdata/styles.css") as f:
            content = f.read()
            zipper.write_contents("styles.css", content, directory="css/")

        with html_writer.HTMLWriter(filepath, "a") as zipper, open("chefdata/scripts.js") as f:
            content = f.read()
            zipper.write_contents("scripts.js", content, directory="js/")

    def to_file(self):
        self.write('<html><head><meta charset="utf-8"><link rel="stylesheet" href="css/styles.css"></head><body><div class="main-content-with-sidebar">'+self.to_html()+'</div><script src="js/scripts.js"></script></body></html>')
        self.write_css_js(self.filepath)

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
        li = ['<ul class="sidebar-items">']
        for e in self.menu.values():
            li.append("<li>")
            #if active_li is not None and e["filename"] == active_li:
            #    li.append('{text}'.format(text=e["text"]))
            #else:
            li.append('<a href="{directory}{filename}" class="sidebar-link">{text}</a>'.format(directory=directory, **e))
            li.append("</li>")
        li.append("</ul>")
        return "".join(li)

    def check(self):
        for name, values in self.menu.items():
            if values["section"] is None:
                raise Exception("{} is not added to a section".format(name))

    def info(self, thumbnail, title, description):
        return dict(
            kind=content_kinds.HTML5,
            source_id=self.filepath,
            title=title,
            description=description,
            license=self.license,
            language=self.lang,
            thumbnail=thumbnail,
            files=[
                dict(
                    file_type=content_kinds.HTML5,
                    path=self.filepath
                )
            ]
        )


class CurriculumType(object):
     def render(self, page, menu_filename, lang="en", resource_url=None):
        for meta_section in self.sections:
            Section = meta_section["class"]
            if isinstance(Section, list):
                section = sum([subsection(page, filename=menu_filename, 
                                menu_name=meta_section["menu_name"], lang=lang,
                                resource_url=resource_url)
                                for subsection in Section])
                section.id = meta_section["id"] 
            else:
                section = Section(page, filename=menu_filename, id_=meta_section["id"], 
                                menu_name=meta_section["menu_name"], lang=lang,
                                resource_url=resource_url)
            yield section


#the ids are fixed by the web page
class Activity(CurriculumType):
    def __init__(self):
        self.name = "Activities"
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
            {"id": "attachments", "class": Attachments, "menu_name": "attachments"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class Lesson(CurriculumType):
    def __init__(self):
        self.name = "Lessons"
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
            {"id": "attachments", "class": Attachments, "menu_name": "attachments"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class CurricularUnit(CurriculumType):
    def __init__(self):
        self.name = "Curricular Units"
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "summary", "class": [CurriculumHeader, Summary, EngineeringConnection], "menu_name": "summary"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "overview", "class": UnitSchedule, "menu_name": "unit_overview"},
            {"id": "schedule", "class": UnitSchedule, "menu_name": "unit_schedule"},
            {"id": "assessment", "class": CollectionSection, "menu_name": "assessment"},
            {"id": "attachments", "class": Attachments, "menu_name": "attachments"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class Sprinkle(CurriculumType):
    def __init__(self):
        self.name = "Sprinkles"
        self.sections = [
            {"id": "quick", "class": QuickLook, "menu_name": "quick_look"},
            {"id": "intro", "class": [CurriculumHeader, Introduction], "menu_name": "introduction"},
            {"id": "sups", "class": CollectionSection, "menu_name": "supplies"},
            {"id": "procedure", "class": CollectionSection, "menu_name": "procedure"},
            {"id": "wrapup", "class": CollectionSection, "menu_name": "wrap_up_-_thought_questions"},
            {"id": "morelikethis", "class": CollectionSection, "menu_name": "more_like_this"},
            {"id": "attachments", "class": Attachments, "menu_name": "attachments"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram, Acknowledgements],
            "menu_name": "info"},
        ]


class MakerChallenge(CurriculumType):
    def __init__(self):
        self.name = "Maker Challenges"
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
            {"id": "attachments", "class": Attachments, "menu_name": "attachments"},
            {"id": "info", "class": [Contributors, Copyright, SupportingProgram],
            "menu_name": "info"},
        ]


class Collection(object):
    def __init__(self, url, source_id, type, title, lang="en", subjects_area=None):
        self.page = self.download_page(url)
        if self.page is not False:
            self.title_prefix = self.clean_title(self.page.find("span", class_="title-prefix"))
            self.title = self.clean_title(self.page.find("span", class_="curriculum-title"))
            if self.title is None:
                self.title = title
            self.contribution_by = None
            self.source_id = source_id
            self.resource_url = url.strip()
            self.type = type
            self.license = None
            self.lang = lang
            self.subjects_area = subjects_area
            
            if self.type == "MakerChallenges":
                self.curriculum_type = MakerChallenge()
            elif self.type == "Lessons":
                self.curriculum_type = Lesson()
            elif self.type == "Activities":
                self.curriculum_type = Activity()
            elif self.type == "CurricularUnits":
                self.curriculum_type = CurricularUnit()
            elif self.type == "Sprinkles":
                self.curriculum_type = Sprinkle()

    def download_page(self, url):
        tries = 0
        while tries < 4:
            try:
                document = downloader.read(url, loadjs=False, session=sess)
            except requests.exceptions.HTTPError as e:
                LOGGER.info("Error: {}".format(e))
            except requests.exceptions.ConnectionError:
                ### this is a weird error, may be it's raised when teachengineering's webpage
                ### is slow to respond requested resources
                LOGGER.info("Connection error, the resource will be scraped in 5s...")
                time.sleep(3)
            else:
                return BeautifulSoup(document, 'html.parser') #html5lib
            tries += 1
        return False


    def description(self):
        descr = self.page.find("meta", property="og:description")
        return descr.get("content", "")

    def clean_title(self, title):
        if title is not None:
            text = title.text.replace("\t", " ")
            return text.strip()

    def drop_null_sections(self, menu):
        sections = []
        for section in self.curriculum_type.render(self.page, menu.filepath, 
                                                    lang=self.lang, 
                                                    resource_url=self.resource_url):
            if section.body is None:
                menu.remove(section.id)
            else:
                sections.append(section)
        return sections

    ##activities, lessons, etc
    def topic_info(self):
        return dict(
                kind=content_kinds.TOPIC,
                source_id=self.type,
                title=self.curriculum_type.name,
                description="",
                license=self.license,
                children=[]
            )

    ##curriculum title
    def info(self, thumbnail):
        return dict(
                kind=content_kinds.TOPIC,
                source_id=self.resource_url,
                title=self.title,
                thumbnail=thumbnail,
                description=self.description(),
                license=self.license,
                children=[]
            )

    def empty_info(self, url):
        return dict(
                kind=content_kinds.TOPIC,
                source_id=url,
                title="TMP",
                thumbnail=None,
                description="",
                license=get_license(licenses.CC_BY, copyright_holder="X").as_dict(),
                children=[]
            )

    def get_thumbnail(self, sections):
        thumbnail_img = None
        for section in sections:
            if section.id == "summary" or section.id == "intro":
                if section.img_url is not None:
                    ext = section.img_url.split(".")[-1]
                    if ext in ['jpg', 'jpeg', 'png']:
                        thumbnail_img = section.img_url #section summary or introduction
                break
        return thumbnail_img

    def get_subjects_area(self):
        if self.subjects_area is None:
            copy_page = copy.copy(self.page)
            ql = QuickLook(copy_page)
            return ql.get_subject_area()
        else:
            return self.subjects_area

    def to_file(self, channel_tree):
        LOGGER.info(" + [{}]: {}".format(self.curriculum_type.name, self.title))
        LOGGER.info("   - URL: {}".format(self.resource_url))
        copy_page = copy.copy(self.page)
        cr = Copyright(copy_page)
        self.license = get_license(licenses.CC_BY, copyright_holder=cr.get_copyright_info()).as_dict()
        subjects_area = self.get_subjects_area()
        base_path = build_path([DATA_DIR, self.type, self.source_id])
        filepath = "{path}/{source_id}.zip".format(path=base_path, 
            source_id=self.source_id)
        menu = Menu(self.page, filepath=filepath, id_="CurriculumNav", 
            exclude_titles=["comments"], #attachments
            include_titles=[("quick", "Quick Look")],
            lang=self.lang)
        menu.add("info", "Info")

        sections = self.drop_null_sections(menu)
        #build the menu index
        menu.to_file()
        #set section's html files to the menu
        for section in sections:
            menu_filename = menu.set_section(section)
            menu_index = menu.to_html(directory="", active_li=menu_filename)
            section.to_file(menu_filename, menu_index=menu_index)

        menu.check()
        menu.license = self.license

        #check for pdfs and videos on all page
        all_sections = CollectionSection(copy_page, resource_url=self.resource_url, lang=self.lang)
        pdfs_info = all_sections.build_pdfs_info(base_path, self.license)
        videos_info = all_sections.build_videos_info(base_path, self.license)

        for subject_area in subjects_area:
            subject_area_topic_node = get_level_map(channel_tree, [subject_area])
            if subject_area_topic_node is None:
                subject_area_topic_node = dict(
                    kind=content_kinds.TOPIC,
                    source_id=subject_area,
                    title=_(subject_area),
                    description="",
                    license=self.license,
                    children=[]
                )
                channel_tree["children"].append(subject_area_topic_node)

            topic_node = get_level_map(channel_tree, [subject_area, self.type])
            thumbnail_img = self.get_thumbnail(sections)
            curriculum_info = self.info(thumbnail_img) #curricular name
            description = self.description()
            curriculum_info["children"].append(menu.info(thumbnail_img, self.title, description))
            if pdfs_info is not None:
                curriculum_info["children"] += pdfs_info
            if videos_info is not None:
                curriculum_info["children"] += videos_info
            if topic_node is None:
                topic_node = self.topic_info() #topic name
                subject_area_topic_node["children"].append(topic_node)

            topic_node["children"].append(curriculum_info)
            if self.type == "CurricularUnits":       
                #build a template for the curriculums
                for url, index in CURRICULAR_UNITS_MAP[self.resource_url].items():
                    #search for lessons
                    node = get_node_from_channel(url, channel_tree, exclude="CurricularUnits")
                    if node is None:
                        curriculum_info["children"].append(self.empty_info(url))
                    else:
                        curriculum_info["children"].append(node)
        
        if self.type != "CurricularUnits":
            curriculars_unit_url = LESSONS_CURRICULAR_MAP.get(self.resource_url, [])
            for curricular_unit_url in curriculars_unit_url:
                #search for curricular units
                curricular_nodes = get_multiple_node_from_channel(curricular_unit_url, 
                    channel_tree, max_level=2)
                if curricular_nodes:
                    for curricular_node in curricular_nodes:
                        for i, children in enumerate(curricular_node["children"]):
                            if children["source_id"] == self.resource_url:
                                curricular_node["children"][i] = curriculum_info
                                break


def get_level_map(tree, levels):
    actual_node = levels[0]
    r_levels = levels[1:]
    for children in tree["children"]:
        if children["source_id"] == actual_node:
            if len(r_levels) >= 1:
                return get_level_map(children, r_levels)
            else:
                return children


def get_node_from_channel(source_id, channel_tree, exclude=None):
    parent = channel_tree["children"]
    while len(parent) > 0:
        for children in parent:
            if children["source_id"] == source_id:
                return children
        nparent = []
        for children in parent:
            try:
                if children["title"] != exclude:
                    nparent.extend(children["children"])
            except KeyError:
                pass
        parent = nparent


def get_multiple_node_from_channel(source_id, channel_tree, exclude=None, max_level=0):
    parent = channel_tree["children"]
    results = []
    level = 1
    while len(parent) > 0:
        for children in parent:
            if children["source_id"] == source_id:
                results.append(children)
        nparent = []
        if level <= max_level:
            for children in parent:
                try:
                    if children["title"] != exclude:
                        nparent.extend(children["children"])
                except KeyError:
                    pass
        level += 1
        parent = nparent
    return results


class CollectionSection(object):
    def __init__(self,  page, filename=None, id_=None, menu_name=None, resource_url=None,
                lang="en"):
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
        self.lang = lang
        self.img_url = None

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
        remove_iframes(self.body)
        remove_links(self.body)
        return "".join([str(p) for p in self.body])

    def get_pdfs(self):
        urls = {}
        if self.body is not None:
            resource_links = self.body.find_all("a", href=re.compile("^\/content|https\:\/\/www.teachengineering"))
            for link in resource_links:
                if link["href"].endswith(".pdf") and link["href"] not in urls:
                    filename = get_name_from_url(link["href"])
                    name = link.text.replace("(pdf)", "").strip()
                    urls[link["href"]] = (filename, name, urljoin(BASE_URL, link["href"]))
            return urls.values()

    def build_pdfs_info(self, path, license=None):
        pdfs_urls = self.get_pdfs()
        if len(pdfs_urls) == 0:
            return

        PDFS_DATA_DIR = build_path([path, 'pdfs'])
        files_list = []
        for filename, name, pdf_url in pdfs_urls:
            try:
                response = downloader.read(pdf_url)
                pdf_filepath = os.path.join(PDFS_DATA_DIR, filename)
                with open(pdf_filepath, 'wb') as f:
                    f.write(response)
                files = dict(
                    kind=content_kinds.DOCUMENT,
                    source_id=pdf_url,
                    title=name,
                    description='',
                    files=[dict(
                        file_type=content_kinds.DOCUMENT,
                        path=pdf_filepath
                    )],
                    language=self.lang,
                    license=license)
                files_list.append(files)
            except requests.exceptions.HTTPError as e:
                LOGGER.info("Error: {}".format(e))

        return files_list

    def get_domain_links(self):
        return set([link.get("href", "") for link in self.body.find_all("a") if link.get("href", "").startswith("/")])

    def images_ref_to_local(self, images_ref, prefix=""):
        images = []
        for img in images_ref:
            if img["src"].startswith("/"):
                img_src = urljoin(BASE_URL, img["src"])
            else:
                img_src = img["src"]
            filename = get_name_from_url(img_src)
            img["src"] = prefix + filename
            images.append((img_src, filename))
        return images

    def get_imgs(self, prefix=""):
        images = self.images_ref_to_local(self.body.find_all("img"), prefix=prefix)
        if len(images) > 0:
            self.img_url = images[-1][0]
        return images

    def get_imgs_into_links(self, prefix=""):
        def check_link(tag):
            allowed_ext = ['jpg', 'jpeg', 'png', 'gif']
            img_ext = tag.attrs.get("href", "").split(".")[-1]
            return tag.name  == "a" and img_ext in allowed_ext
        return [a.get("href", None) for a in self.body.find_all(check_link)]

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

    def build_videos_info(self, path, license=None):
        videos_urls = self.get_videos_urls()
        if len(videos_urls) == 0:
            return

        VIDEOS_DATA_DIR = build_path([path, 'videos'])
        videos_list = []

        for i, url in enumerate(videos_urls):
            resource = YouTubeResource(url, lang=self.lang)
            resource.to_file(filepath=VIDEOS_DATA_DIR)
            if resource.resource_file is not None:
                videos_list.append(resource.resource_file)
        return videos_list

    def write(self, filename, content):
        with html_writer.HTMLWriter(self.filename, "a") as zipper:
            zipper.write_contents(filename, content, directory="files")

    def write_img(self, url, filename):
        with html_writer.HTMLWriter(self.filename, "a") as zipper:
            zipper.write_url(url, filename, directory="files")

    def to_file(self, filename, menu_index=None):
        if self.body is not None and filename is not None:
            images = self.get_imgs()
            content = self.get_content()

            if menu_index is not None:
                html = '<html><head><meta charset="utf-8"><link rel="stylesheet" href="../css/styles.css"></head><body><div class="sidebar"><a class="sidebar-link toggle-sidebar-button" href="javascript:void(0)" onclick="javascript:toggleNavMenu();">&#9776;</a>{}</div><div class="main-content-with-sidebar">{}</div><script src="../js/scripts.js"></script></body></html>'.format(
                    menu_index, content)
            else:
                html = '<html><head><meta charset="utf-8"><link rel="stylesheet" href="../css/styles.css"></head><body><div class="main-content-with-sidebar">{}</div><script src="../js/scripts.js"></script></body></html>'.format(
                    content)

            self.write(filename, html)
            for img_src, img_filename in images:
                self.write_img(img_src, img_filename)


class CurriculumHeader(CollectionSection):
    def __init__(self, page, filename=None, id_="curriculum-header", 
                menu_name="summary", lang="en", resource_url=None):
        self.body = page.find("div", class_="curriculum-header")
        self.filename = filename
        self.menu_name = menu_name
        self.id = id_
        self.img_url = None
        self.resource_url = resource_url


class QuickLook(CollectionSection):
    def __init__(self, page, filename=None, id_="quick", menu_name="quick_look", 
            lang="en", resource_url=None):
        super(QuickLook, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        self.body = page.find("div", class_="quick-look")
        ## cleaning html code
        for s in self.body.find_all("script"):
            s.extract()
        for b in self.body.find_all("button"):
            b.extract()
        div = self.body.find("div", id="PrintShareModal")
        div.extract()

    def get_subject_area(self):
        subject_areas = self.body.find_all(lambda tag: tag.name == 'a' and\
                        tag.findParent("dd", class_="subject-area"))
        subjects = []
        for a in subject_areas:
            subjects.append(a.text)
        return subjects


class UnitSchedule(CollectionSection):
    def __init__(self, page, filename=None, id_=None, 
                menu_name=None, lang="en", resource_url=None):
        super(UnitSchedule, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        if self.body is not None:
            self.get_schedule(self.body.find_all("a"))

    def get_schedule(self, data_list):
        if data_list is not None:
            for a in data_list:
                relative_url = a.get("href", "")
                if relative_url.startswith("/") and "curricularunits" not in relative_url\
                    and not relative_url.endswith("pdf"):
                    lesson_url = urljoin(BASE_URL, a["href"]).strip()
                    if lesson_url not in CURRICULAR_UNITS_MAP[self.resource_url]:               
                        CURRICULAR_UNITS_MAP[self.resource_url][lesson_url] = len(CURRICULAR_UNITS_MAP[self.resource_url])
                    LESSONS_CURRICULAR_MAP[lesson_url].add(self.resource_url)


class EngineeringConnection(CollectionSection):
    def __init__(self, page, filename=None, id_="engineering_connection", 
                menu_name="engineering_connection", lang="en", resource_url=None):
        super(EngineeringConnection, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Engineering Connection\s*")))


class Summary(CollectionSection):
    def __init__(self, page, filename=None, id_="summary", menu_name="summary", 
                lang="en", resource_url=None):
        super(Summary, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)


class Introduction(CollectionSection):
    def __init__(self, page, filename=None, id_="intro", menu_name="introduction", 
                lang="en", resource_url=None):
        super(Introduction, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)


class Attachments(CollectionSection):
    def __init__(self, page, filename=None, id_="attachments", 
                menu_name="attachments", lang="en", resource_url=None):
        super(Attachments, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)

    def get_content(self):
        remove_iframes(self.body)
        remove_links(self.body)
        for div in self.body.find_all("div"):
            if div.text.find("pdf") == -1:
                div.extract()
            else:
                div.replace_with(div.text + " " + "Look for the document in current folder")
        return "".join([str(p) for p in self.body])


class Contributors(CollectionSection):
    def __init__(self, page, filename=None, id_="contributors", 
            menu_name="contributors", lang="en", resource_url=None):
        super(Contributors, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Contributors\s*")))


class SupportingProgram(CollectionSection):
    def __init__(self, page, filename=None, id_="supporting_program", 
                menu_name="supporting_program", lang="en", resource_url=None):
        super(SupportingProgram, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Supporting Program\s*")))


class Acknowledgements(CollectionSection):
    def __init__(self, page, filename=None, id_="acknowledgements", 
                menu_name="acknowledgements", lang="en", resource_url=None):
        super(Acknowledgements, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
        self.body = page.find(lambda tag: tag.name=="section" and\
            tag.findChildren("h3", text=re.compile("\s*Acknowledgements\s*")))


class Copyright(CollectionSection):
    def __init__(self, page, filename=None, id_="copyright", menu_name="copyright", 
                lang="en", resource_url=None):
        super(Copyright, self).__init__(page, filename=filename,
                id_=id_, menu_name=menu_name, lang=lang, resource_url=resource_url)
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
    def __init__(self, resource_url=None, type_name=None):
        self.resource_url = resource_url
        self.type_name = type_name
        self.resource_file = None
        LOGGER.info("Resource Type: {} [{}]".format(type_name, self.resource_url))

    def to_file(self, filepath=None):
        pass

    def add_resource_file(self, info):
        self.resource_file = info


class ImagesListResource(object):
    def __init__(self, resource_urls, filepath, title):
        self.urls = self.clean_urls(resource_urls)
        self.prefix = "files"
        self.filepath = filepath
        self.title = title

    def clean_urls(self, urls):
        cleaned_urls = []
        for url in urls:
            if url.startswith("/"):
                cleaned_urls.append(urljoin(BASE_URL, url))
            else:
                cleaned_urls.append(url)
        return cleaned_urls

    def menu(self):
        tag = '<ul class="sidebar-items">'
        for url in self.urls:
            tag += '<li><a href="{}/{}" class="sidebar-link">{}</a></li>'.format(
                self.prefix, get_name_from_url(url), get_name_from_url_no_ext(url))
        tag += "</ul>"
        return tag

    def write_img(self, url, filename):
        with html_writer.HTMLWriter(self.filepath, "a") as zipper:
            zipper.write_url(url, filename, directory=self.prefix)

    def write(self, content):
        with html_writer.HTMLWriter(self.filepath, "w") as zipper:
            zipper.write_index_contents(content)

    def to_file(self):
        content = self.menu()
        html = '<html><head><meta charset="utf-8"><link rel="stylesheet" href="css/styles.css"></head><body><div class="main-content-with-sidebar">{}</div><script src="scripts.js"></script></body></html>'.format(
            content)
        self.write(content)
        for img_url in self.urls:
            self.write_img(img_url, get_name_from_url(img_url))

    def info(self):
        return dict(
                kind=content_kinds.HTML5,
                source_id=self.filepath,
                title="Images " + self.title,
                description="",
                license=get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict(),
                language="en",
                thumbnail=None,
                files=[
                    dict(
                        file_type=content_kinds.HTML5,
                        path=self.filepath
                    )
                ])


class YouTubeResource(ResourceType):
    def __init__(self, resource_url, type_name="Youtube", lang="en"):
        super(YouTubeResource, self).__init__(resource_url=self.clean_url(resource_url), 
            type_name=type_name)
        self.file_format = file_formats.MP4
        self.lang = lang
        self.filepath = None
        self.filename = None

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

    def get_video_info(self, download_to=None, subtitles=True):
        ydl_options = {
                'writesubtitles': subtitles,
                'allsubtitles': subtitles,
                'no_warnings': True,
                'restrictfilenames':True,
                'continuedl': True,
                'quiet': False,
                'format': "bestvideo[height<={maxheight}][ext=mp4]+bestaudio[ext=m4a]/best[height<={maxheight}][ext=mp4]".format(maxheight='480'),
                'outtmpl': '{}/%(id)s'.format(download_to),
                'noplaylist': False
            }

        with youtube_dl.YoutubeDL(ydl_options) as ydl:
            try:
                ydl.add_default_info_extractors()
                info = ydl.extract_info(self.resource_url, download=(download_to is not None))
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
        subs = []
        if 'subtitles' in video_info:
            subtitles_info = video_info["subtitles"]
            for language in subtitles_info.keys():
                subs.append(dict(file_type=SUBTITLES_FILE, youtube_id=video_id, language=language))
        return subs

    def process_file(self, download=False, filepath=None):
        self.download(download=download, base_path=filepath)
        if self.filepath:
            files = [dict(file_type=content_kinds.VIDEO, path=self.filepath)]
            files += self.subtitles_dict()

            self.add_resource_file(dict(
                kind=content_kinds.VIDEO,
                source_id=self.resource_url,
                title=self.filename,
                description='',
                files=files,
                language=self.lang,
                license=get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict()))

    def download(self, download=True, base_path=None):
        if not "watch?" in self.resource_url or "/user/" in self.resource_url or\
            download is False:
            return

        download_to = base_path
        for i in range(4):
            try:
                info = self.get_video_info(download_to=download_to, subtitles=False)
                if info is not None:
                    LOGGER.info("Video resolution: {}x{}".format(info.get("width", ""), info.get("height", "")))
                    self.filepath = os.path.join(download_to, "{}.mp4".format(info["id"]))
                    self.filename = info["title"]
                    if self.filepath is not None and os.stat(self.filepath).st_size == 0:
                        LOGGER.info("Empty file")
                        self.filepath = None
            except (ValueError, IOError, OSError, URLError, ConnectionResetError) as e:
                LOGGER.info(e)
                LOGGER.info("Download retry")
                time.sleep(.8)
            except (youtube_dl.utils.DownloadError, youtube_dl.utils.ContentTooShortError,
                    youtube_dl.utils.ExtractorError, OSError) as e:
                LOGGER.info("An error ocurred, may be the video is not available.")
                return
            except OSError:
                return
            else:
                return

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

def remove_iframes(content):
    if content is not None:
        for iframe in content.find_all("iframe"):
            iframe.extract()


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


class LivingLabs(Collection):
    def __init__(self):
        url = urljoin(BASE_URL, 'livinglabs')
        super(LivingLabs, self).__init__(url, "LivingLabs", "LivingLabs", "", lang="en", subjects_area=None)
        self.license = get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict()

    def info(self, description, sections):
        info = dict(
            kind=content_kinds.TOPIC,
            source_id=self.resource_url,
            title="Living Labs",
            thumbnail=None,
            description=description,
            license=self.license,
            children=[]
        )
        
        for section in sections:
            section_info = dict(
                kind=content_kinds.TOPIC,
                source_id=section["resource_url"],
                title=section["title"],
                thumbnail=section["thumbnail"],
                description=section["description"],
                license=self.license,
                children=section["resources"]
            )
            info["children"].append(section_info)

        return info

    def sections(self, channel_tree):
        sections = []
        for a in self.page.find_all(lambda tag: tag.name == "a" and tag.findParent("h3")):
            url = urljoin(BASE_URL, a.get("href", ""))
            section_info = {"resource_url": url, "title": a.text, "description": "", "thumbnail": None}
            sections.append(section_info)

        descriptions = []
        for descr in self.page.find_all(lambda tag: tag.name == "p" and tag.findParent("div", class_="row")):
            descriptions.append(descr.text.replace("\r", "").replace("\n", "").strip())

        for row in self.page.find_all("div", class_="row"):
            img_url = row.find("img")
            a = row.find("a")
            if a is not None:
                for section in sections:
                    if section["title"] == a.text and img_url is not None:
                        section["thumbnail"] = urljoin(BASE_URL, img_url.get("src", None))

        for section, description in zip(sections, descriptions[1:]):
            section["description"] = description
        
        all_sections = CollectionSection(self.page, resource_url=self.resource_url, lang=self.lang)
        base_path = build_path([DATA_DIR, self.type])
        sections_files = self.build_sections_data(base_path, sections, channel_tree)
        for section, files_info in zip(sections, sections_files):
            section["resources"] = files_info

        info = self.info(descriptions[0], sections)
        videos_info = all_sections.build_videos_info(base_path, self.license)        
        if videos_info is not None:
            info["children"] += videos_info
        return info

    def build_sections_data(self, base_path, sections, channel_tree):
        for section in sections:
            collection = Collection(section["resource_url"], source_id=section["title"], 
                        type=section["title"], title=section["title"])
            filepath = "{path}/{source_id}.zip".format(path=base_path, 
                source_id=section["title"])
            collection_section = LivingLabsSection(collection, filename=filepath, 
                base_path=base_path)
            lessons_main_page = list(attach_curriculums_from_urls(
                        collection_section.get_curriculums(), channel_tree))
            sub_pages = collection_section.get_domain_links()
            collection_section.to_file()
            menu_info = dict(
                kind=content_kinds.HTML5,
                source_id=filepath,
                title=collection.title,
                description="",
                license=self.license,
                language=self.lang,
                thumbnail=None,
                files=[
                    dict(
                        file_type=content_kinds.HTML5,
                        path=filepath
                    )
                ]
            )
            resources = []
            for i, sub_page in enumerate(sub_pages):
                if sub_page.startswith("/livinglabs/"):
                    page_name = sub_page.split("/")[-1]
                    url = urljoin(BASE_URL, sub_page)
                    source_id = section["title"]+"_"+str(i)
                    base_path = build_path([DATA_DIR, "LivingLabs", page_name])
                    filename = "{path}/{source_id}.zip".format(path=base_path, 
                        source_id="{}_data".format(source_id))
                    collection = Collection(url, source_id=source_id, 
                        type="LivingLabResources", title=page_name)
                    resources_section = LivingLabsSection(collection, 
                        base_path=base_path, filename=filename)
                    lessons = list(attach_curriculums_from_urls(
                        resources_section.get_curriculums(), channel_tree))
                    resources_info = resources_section.resources()
                    if resources_info is not None:
                        resources.extend(resources_info)
                    if len(lessons) > 0:
                        resources.extend(lessons)

            if len(lessons_main_page) > 0:
                resources.extend(lessons_main_page)

            if len(resources) > 0:
                resource_topic = dict(
                    kind=content_kinds.TOPIC,
                    source_id="Resources",
                    title=_("Resources"),
                    description="",
                    license=self.license,
                    language=self.lang,
                    thumbnail=None,
                    children=resources
                )
                resource_topic_l = [resource_topic]
            else:
                resource_topic_l = []
            yield [menu_info] + resource_topic_l
            

class LivingLabsSection(CollectionSection):
    def __init__(self,  collection, filename=None, id_=None, menu_name=None, base_path=None):
        page = collection.page.find("div", class_="page-wrapper")
        resource_url = collection.resource_url
        lang = collection.lang
        super(LivingLabsSection, self).__init__(page, filename=filename, id_=id_, 
            menu_name=menu_name, resource_url=resource_url, lang=lang)
        self.base_path = base_path
        self.license = get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict()
        self.collection = collection
        LOGGER.info(" + [{}]: {}".format(self.collection.type, self.collection.title))
        LOGGER.info("   - URL: {}".format(self.collection.resource_url))

    def get_curriculums(self):
        def check_curriculum(tag):
            return tag.name == "a" and tag.attrs.get("href", "").startswith("/activities/") or\
            tag.attrs.get("href", "").startswith("/lessons/")
        links = self.body.find_all(check_curriculum)
        return [urljoin(BASE_URL, a.get("href", "").strip()) for a in links]

    def resources(self):
        img_filepath = "{path}/{source_id}_img.zip".format(path=self.base_path, 
            source_id=self.collection.source_id)
        img = ImagesListResource(self.get_imgs_into_links(), filepath=img_filepath, 
            title=self.collection.title)
        videos_info = self.build_videos_info(self.base_path, self.license)
        info = [self.info()]
        if videos_info is not None:
            info += videos_info
        if len(img.urls) > 0:
            img.to_file()
            info.append(img.info())
        self.to_file()
        return info

    def info(self):
        return dict(
                kind=content_kinds.HTML5,
                source_id=self.filename,
                title=self.collection.title,
                description="",
                license=self.license,
                language=self.lang,
                thumbnail=None,
                files=[
                    dict(
                        file_type=content_kinds.HTML5,
                        path=self.filename
                    )
                ]
            )
    
    def write_css_js(self, filepath):
        with html_writer.HTMLWriter(filepath, "a") as zipper, open("chefdata/styles.css") as f:
            content = f.read()
            zipper.write_contents("styles.css", content, directory="css/")

        with html_writer.HTMLWriter(filepath, "a") as zipper, open("chefdata/scripts.js") as f:
            content = f.read()
            zipper.write_contents("scripts.js", content, directory="js/")

    def write_img(self, url, filename):
        with html_writer.HTMLWriter(self.filename, "a") as zipper:
            zipper.write_url(url, filename, directory="files")

    def write(self, content):
        with html_writer.HTMLWriter(self.filename, "w") as zipper:
            zipper.write_index_contents(content)

    def to_file(self):
        if self.body is not None:
            images = self.get_imgs(prefix="files/")
            content = self.get_content()
            html = '<html><head><meta charset="utf-8"><link rel="stylesheet" href="css/styles.css"></head><body><div class="main-content-with-sidebar">{}</div></body></html>'.format(
                content)
            self.write(html)
            self.write_css_js(self.filename)
            for img_src, img_filename in images:
                self.write_img(img_src, img_filename)


def attach_curriculums_from_urls(links, channel_tree):
    curriculums = defaultdict(list)
    for url in links:
        node = get_node_from_channel(url, channel_tree, exclude="CurricularUnits")
        if url.find("activities") != -1:
            source_id = "Activities"
        else:
            source_id = "Lessons"
        if node is not None:
            curriculums[source_id].append(node)

    for curriculum_type, children in curriculums.items():
        if len(children) > 0:
            yield dict(
                kind=content_kinds.TOPIC,
                source_id=curriculum_type,
                title=curriculum_type,
                description="",
                license=get_license(licenses.CC_BY, copyright_holder=GENERAL_COPYRIGHT_HOLDER).as_dict(),
                children=children
            ) 


def save_thumbnail():
    url = "https://scontent.xx.fbcdn.net/v/t1.0-1/p50x50/10492197_815509258473514_3497726003055575270_n.jpg?oh=bfcd61aebdb3d2265c31c2286290bd31&oe=5B1FACCA"
    THUMB_DATA_DIR = build_path([DATA_DIR, 'thumbnail'])
    filepath = os.path.join(THUMB_DATA_DIR, "TELogoNew.jpg")
    document = downloader.read(url, loadjs=False, session=sess)        
    with open(filepath, 'wb') as f:
        f.write(document)
        return filepath


class TeachEngineeringChef(JsonTreeChef):
    ROOT_URL = "https://{HOSTNAME}"
    HOSTNAME = "teachengineering.org"
    TREES_DATA_DIR = os.path.join(DATA_DIR, 'trees')
    CRAWLING_STAGE_OUTPUT_TPL = 'web_resource_tree_{}.json'
    SCRAPING_STAGE_OUTPUT_TPL = 'ricecooker_json_tree_{}.json'
    LICENSE = get_license(licenses.CC_BY, copyright_holder="The source of this material is the TeachEngineering digital library collection at www.TeachEngineering.org. All rights reserved.").as_dict()
    #THUMBNAIL = 'https://www.teachengineering.org/images/logos/v-636511398960000000/TELogoNew.png'

    def __init__(self):
        build_path([TeachEngineeringChef.TREES_DATA_DIR])
        self.thumbnail = save_thumbnail()
        super(TeachEngineeringChef, self).__init__()

    def download_css_js(self):
        r = requests.get("https://raw.githubusercontent.com/learningequality/html-app-starter/master/css/styles.css")
        with open("chefdata/styles.css", "wb") as f:
            f.write(r.content)

        r = requests.get("https://raw.githubusercontent.com/learningequality/html-app-starter/master/js/scripts.js")
        with open("chefdata/scripts.js", "wb") as f:
            f.write(r.content)

    def pre_run(self, args, options):
        css = os.path.join(os.path.dirname(os.path.realpath(__file__)), "chefdata/styles.css")
        js = os.path.join(os.path.dirname(os.path.realpath(__file__)), "chefdata/scripts.js")
        if not if_file_exists(css) or not if_file_exists(js):
            LOGGER.info("Downloading styles")
            self.download_css_js()
        self.crawl(args, options)
        self.scrape(args, options)
        #test()

    def crawl(self, args, options):
        web_resource_tree = dict(
            kind='TeachEngineeringResourceTree',
            title='TeachEngineering',
            children=[]
        )
        lang = options.get('lang', 'en')
        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR,                     
                                    TeachEngineeringChef.CRAWLING_STAGE_OUTPUT_TPL.format(lang))
        curriculum_url = urljoin(TeachEngineeringChef.ROOT_URL.format(HOSTNAME=TeachEngineeringChef.HOSTNAME), "curriculum/browse")
        resource_browser = ResourceBrowser(curriculum_url)
        for data in resource_browser.run():
            web_resource_tree["children"].append(data)
        with open(crawling_stage, 'w') as f:
            json.dump(web_resource_tree, f, indent=2)
        return web_resource_tree

    def scrape(self, args, options):
        lang = options.get('lang', 'en')
        download_video = options.get('--download-video', "1")
        if int(download_video) == 0:
            global DOWNLOAD_VIDEOS
            DOWNLOAD_VIDEOS = False

        crawling_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.CRAWLING_STAGE_OUTPUT_TPL.format(lang))
        with open(crawling_stage, 'r') as f:
            web_resource_tree = json.load(f)
            assert web_resource_tree['kind'] == 'TeachEngineeringResourceTree'
         
        translation = gettext.translation('subjects', 'lang/', languages=[lang])
        translation.install()
        global _ 
        _ = translation.gettext

        if lang == 'es':
            channel_tree = self._build_scraping_json_tree_es(web_resource_tree)
        else:
            channel_tree = self._build_scraping_json_tree(web_resource_tree)
        
        self.write_tree_to_json(channel_tree, lang)

    def write_tree_to_json(self, channel_tree, lang):
        scrape_stage = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                                TeachEngineeringChef.SCRAPING_STAGE_OUTPUT_TPL.format(lang))
        write_tree_to_json_tree(scrape_stage, channel_tree)

    def get_json_tree_path(self, **kwargs):
        lang = kwargs.get('lang', "en")
        json_tree_path = os.path.join(TeachEngineeringChef.TREES_DATA_DIR, 
                    TeachEngineeringChef.SCRAPING_STAGE_OUTPUT_TPL.format(lang))
        return json_tree_path

    def _build_scraping_json_tree(self, web_resource_tree):
        LANG = 'en'
        channel_tree = dict(
            source_domain=TeachEngineeringChef.HOSTNAME,
            source_id='teachengineering',
            title='TeachEngineering',
            description="""The TeachEngineering digital library is a collaborative project between faculty, students and teachers associated with five founding partner universities, with National Science Foundation funding. The collection continues to grow and evolve with new additions submitted from more than 50 additional contributor organizations, a cadre of volunteer teacher and engineer reviewers, and feedback from teachers who use the curricula in their classrooms."""[:400], #400 UPPER LIMIT characters allowed 
            thumbnail=self.thumbnail,
            language=LANG,
            children=[],
            license=TeachEngineeringChef.LICENSE,
        )
        #counter = 0
        for resource in web_resource_tree["children"]:
            collection = Collection(resource["url"],
                            source_id=resource["id"],
                            type=resource["collection"],
                            title=resource["title"],
                            lang=LANG)
            collection.to_file(channel_tree)
            #if counter == 0:
            #    break
            #counter += 1
        living_labs = LivingLabs()
        channel_tree["children"].append(living_labs.sections(channel_tree))
        return channel_tree

    def _build_scraping_json_tree_es(self, web_resource_tree):
        LANG = 'es'
        channel_tree = dict(
            source_domain=TeachEngineeringChef.HOSTNAME,
            source_id='teachengineering_es',
            title='TeachEngineering (es)',
            description="""La biblioteca digital de TeachEngineering es un proyecto colaborativo entre académicos, estudiantes and profesores, asociados con cinco universidades como socios fundadores con los fondos de la Fundación Nacional de Ciencia. La colección continua creciendo y desarrollando nuevas adiciones enviadas desde más de 50 organizaciones colaboradoras, un voluntariado de profesores e ingenieros que revisan el contenido, y profesores que desde sus salones de clase utilizan y dan retroalimentación a los planes de estudio."""[:400], #400 UPPER LIMIT characters allowed 
            thumbnail=self.thumbnail,
            language=LANG,
            children=[],
            license=TeachEngineeringChef.LICENSE,
        )
        for resource in web_resource_tree["children"]:
            if resource["spanishVersionId"] is not None:
                collection_en = Collection(resource["url"],
                        source_id=resource["id"],
                        type=resource["collection"],
                        title=resource["title"])
                
                collection = Collection(resource["url_es"],
                        source_id=resource["spanishVersionId"],
                        type=resource["collection"],
                        title=resource["title"],
                        lang=LANG,
                        subjects_area=collection_en.get_subjects_area())
                collection.to_file(channel_tree)
        return channel_tree


# CLI: This code will run when `souschef.py` is called on the command line
################################################################################
if __name__ == '__main__':
    chef = TeachEngineeringChef()
    chef.main()
