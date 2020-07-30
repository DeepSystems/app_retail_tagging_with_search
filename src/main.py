import sys
import json
import os
import threading
from collections import defaultdict
from pandas import read_excel
import numpy as np
import supervisely_lib as sly

my_app = sly.AppService()

REMOTE_DIRECTORY_PATH = "/upc_references"
LOCAL_DIRECTORY_PATH = os.path.join(my_app.data_dir, REMOTE_DIRECTORY_PATH[1:])
sly.fs.ensure_base_path(LOCAL_DIRECTORY_PATH)

FNAME_URL = "upc_ref_url.json"
FNAME_RES_UPC_BATCHES = "res_upc_batches.json"
FNAME_RES_USER_UPC_BATCHES = "res_user_upc_batches.json"
FNAME_CATALOG = "product_catalog.xlsx"
TAG_NAME = 'UPC CODE'

PRODUCT_CLASS_NAME = "Product"

user2upc = defaultdict(list)
upc2catalog = defaultdict(dict)
upc_gallery = defaultdict(list)
full_catalog = []

anns = {}
anns_lock = threading.Lock()
metas = {}
metas_lock = threading.Lock()


def get_annotation(api: sly.Api, project_id, image_id, figure_id=None):
    def _download_ann(force=False):
        meta = get_project_meta(api, project_id, force)
        ann_json = api.annotation.download(image_id).annotation
        ann = sly.Annotation.from_json(ann_json, meta)

        global anns_lock
        anns_lock.acquire()
        anns[image_id] = ann
        anns_lock.release()

    global anns
    if image_id not in anns:
        _download_ann()

    if figure_id is not None:
        ids = [label.geometry.sly_id for label in anns[image_id].labels]
        if figure_id not in ids:
            _download_ann(force=True)

    return anns[image_id]


def get_project_meta(api: sly.Api, project_id, force=False):
    global metas
    if project_id not in metas or force is True:
        meta_json = api.project.get_meta(project_id)
        meta = sly.ProjectMeta.from_json(meta_json)

        upc_tag_meta = meta.get_tag_meta(TAG_NAME)
        if upc_tag_meta is None:
            meta = meta.add_tag_meta(sly.TagMeta(TAG_NAME, sly.TagValueType.ANY_STRING))
            api.project.update_meta(project_id, meta.to_json())

            # get meta from server again to access tag_id (tag_meta_id)
            meta_json = api.project.get_meta(project_id)
            meta = sly.ProjectMeta.from_json(meta_json)

        global metas_lock
        metas_lock.acquire()
        metas[project_id] = meta
        metas_lock.release()

    return metas[project_id]


def get_first_id(ann: sly.Annotation):
    for idx, label in enumerate(ann.labels):
        if label.obj_class.name == PRODUCT_CLASS_NAME:
            return label.geometry.sly_id
    return None


def get_prev_id(ann: sly.Annotation, active_figure_id):
    prev_idx = None
    for idx, label in enumerate(ann.labels):
        if label.geometry.sly_id == active_figure_id:
            if prev_idx is None:
                return None
            return ann.labels[prev_idx].geometry.sly_id
        if label.obj_class.name == PRODUCT_CLASS_NAME:
            prev_idx = idx


def get_next_id(ann: sly.Annotation, active_figure_id):
    need_search = False
    for idx, label in enumerate(ann.labels):
        if label.geometry.sly_id == active_figure_id:
            need_search = True
            continue
        if need_search:
            if label.obj_class.name == PRODUCT_CLASS_NAME:
                return label.geometry.sly_id
    return None


def select_object(api: sly.Api, task_id, context, find_func, show_msg=False):
    user_id = context["userId"]
    image_id = context["imageId"]
    project_id = context["projectId"]
    ann_tool_session = context["sessionId"]

    ann = get_annotation(api, project_id, image_id)

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        active_figure_id = get_first_id(ann)
    else:
        active_figure_id = find_func(ann, active_figure_id)
        if show_msg is True and active_figure_id is None:
            api.app.set_field(task_id, "state.dialogVisible", True)

    if active_figure_id is not None:
        api.img_ann_tool.set_figure(ann_tool_session, active_figure_id)
        api.img_ann_tool.zoom_to_figure(ann_tool_session, active_figure_id, 2)


@my_app.callback("prev_object")
@sly.timeit
def prev_object(api: sly.Api, task_id, context, state, app_logger):
    select_object(api, task_id, context, get_prev_id)


@my_app.callback("next_object")
@sly.timeit
def next_object(api: sly.Api, task_id, context, state, app_logger):
    select_object(api, task_id, context, get_next_id, show_msg=True)

def _assign_tag(api, context, selected_upc):
    project_id = context["projectId"]
    meta = get_project_meta(api, project_id)

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        sly.logger.warn("Figure is not selected.")

    tag_meta = meta.get_tag_meta(TAG_NAME)
    api.advanced.add_tag_to_object(tag_meta.sly_id, active_figure_id, value=selected_upc)

@my_app.callback("assign_tag")
@sly.timeit
def assign_tag(api: sly.Api, task_id, context, state, app_logger):
    global user2upc
    user_id = context["userId"]
    user2selectedUpc = state["user2selectedUpc"]
    selected_tag_index = user2selectedUpc[str(user_id)]
    selected_upc = user2upc[user_id][selected_tag_index]["upc"]
    _assign_tag(api, context, selected_upc)

@my_app.callback("assign_tag_catalog")
@sly.timeit
def assign_tag_catalog(api: sly.Api, task_id, context, state, app_logger):
    user_id = context["userId"]
    selected_upc = state["user2selectedRowData"][str(user_id)]['UPC CODE']
    _assign_tag(api, context, selected_upc)

def _multi_assign_tag(api, context, selected_upc):
    project_id = context["projectId"]
    image_id = context["imageId"]
    meta = get_project_meta(api, project_id)

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        sly.logger.warn("Figure is not selected.")

    ann = get_annotation(api, project_id, image_id, active_figure_id)
    selected_label = None
    for label in ann.labels:
        if label.geometry.sly_id == active_figure_id:
            selected_label = label
            break

    tag_meta = meta.get_tag_meta(TAG_NAME)
    for idx, label in enumerate(ann.labels):
        if label.geometry.to_bbox().intersects_with(selected_label.geometry.to_bbox()):
            api.advanced.add_tag_to_object(tag_meta.sly_id, label.geometry.sly_id, value=selected_upc)

@my_app.callback("multi_assign_tag")
@sly.timeit
def multi_assign_tag(api: sly.Api, task_id, context, state, app_logger):
    global user2upc
    user_id = context["userId"]
    user2selectedUpc = state["user2selectedUpc"]
    selected_tag_index = user2selectedUpc[str(user_id)]
    selected_upc = user2upc[user_id][selected_tag_index]["upc"]
    _multi_assign_tag(api, context, selected_upc)


@my_app.callback("multi_assign_tag_catalog")
@sly.timeit
def multi_assign_tag_catalog(api: sly.Api, task_id, context, state, app_logger):
    user_id = context["userId"]
    selected_upc = state["user2selectedRowData"][str(user_id)]['UPC CODE']
    _multi_assign_tag(api, context, selected_upc)


def download_remote_files(api, team_id):
    sly.fs.ensure_base_path(LOCAL_DIRECTORY_PATH)
    for fname in [FNAME_URL, FNAME_RES_UPC_BATCHES, FNAME_RES_USER_UPC_BATCHES, FNAME_CATALOG]:
        remote_path = os.path.join(REMOTE_DIRECTORY_PATH, fname)
        if not api.file.exists(team_id, remote_path):
            raise FileExistsError("File {!r} does not exist".format(remote_path))
        local_path = os.path.join(LOCAL_DIRECTORY_PATH, fname)
        api.file.download(team_id, remote_path, local_path)

def init_user_2_upc(api, team_id):
    upc_url = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_URL))
    upc_batch = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_RES_UPC_BATCHES))
    user_upc_batch = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_RES_USER_UPC_BATCHES))

    global user2upc, upc_gallery

    for user, upc_batches in user_upc_batch.items():
        user_info = api.user.get_member_info_by_login(team_id, user)
        if user_info is None:
            team_info = api.team.get_info_by_id(team_id)
            raise RuntimeError("User {!r} no found in team {!r}".format(user, team_info.name))
        for batch_id in upc_batches:
            for upc_code in upc_batch[str(batch_id)]:
                first_url = True
                for url in upc_url[upc_code]:
                    upc_gallery[upc_code].append([url])
                    if "_full" in url:
                        continue
                    if first_url is False:
                        continue
                    first_url = False
                    user2upc[user_info.id].append({"upc": upc_code, "image_url": url})

def init_catalog():
    global upc2catalog
    sheets = read_excel(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_CATALOG), sheet_name=None)
    catalog = sheets[list(sheets.keys())[0]]  # get first sheet from excel
    sly.logger.info("Size of catalog: {}".format(len(catalog)))
    upcs = list(catalog["UPC CODE"])
    # upc = '7861042566762'
    for upc in upcs:
        res = catalog[catalog['UPC CODE'] == np.int64(upc)]
        info = json.loads(res.to_json(orient="records"))
        if len(info) != 1:
            info = {}
        else:
            info = info[0]
        upc2catalog[upc] = info

def init_search_catalog():
    global upc2catalog
    global full_catalog
    global upc_gallery

    popover_id = 1
    for upc_int64, info in upc2catalog.items():
        link = None
        if str(upc_int64) in upc_gallery:
            link = upc_gallery[str(upc_int64)][0][0]
        else:
            upc_gallery[str(upc_int64)] = []

        new_info = info.copy()
        if link is not None:
            # element = """
            # <el-popover
            #   ref="popover{}"
            #   placement="left"
            #   trigger="hover"
            #   content="this is content, this is content, this is content">
            # </el-popover>
            # """.format(popover_id)
            # #button = "<el-button v-popover:popover{} size=\"mini\">ref image</el-button>".format(popover_id)
            # popover_id += 1
            # button = "<el-button size=\"mini\">ref image</el-button>"

            new_info["image"] = '<img style="height:80px; width:auto;" src="{}"/>'.format(link)

            # element = """
            # <div class="popover-wrapper">
            #     <div class="trigger">hover me</div>
            #     <div class="popover-content">
            #       <img src="{}" />
            #     </div>
            # </div>
            # """.format(link)
            # new_info["image"] = element

        # skip upc code
        else:
            new_info["image"] = ""
        full_catalog.append(new_info)


def main():
    api = sly.Api.from_env()

    team_id = os.environ["TEAM_ID"]
    download_remote_files(api, team_id)
    init_user_2_upc(api, team_id)
    init_catalog()

    user2selectedUpc = {}
    for key, value in user2upc.items():
        user2selectedUpc[key] = 0

    user2upcIndex2Info = defaultdict(dict)
    for user_id, upcs in user2upc.items():
        for idx, upc_link in enumerate(upcs):
            info = upc2catalog[np.int64(upc_link["upc"])]
            user2upcIndex2Info[user_id][idx] = info

    user2upcIndex2upcGallery = defaultdict(lambda: defaultdict(dict))
    for user_id, upcs in user2upc.items():
        for idx, upc_link in enumerate(upcs):
            g = upc_gallery[upc_link["upc"]]
            user2upcIndex2upcGallery[user_id][idx] = g

    init_search_catalog()
    global full_catalog

    user2selectedRowData = {}
    for key, _ in user2upc.items():
        user2selectedRowData[key] = full_catalog[0]

    data = {
        "user2upc": user2upc,
        "user2upcIndex2Info": user2upcIndex2Info,
        "user2upcIndex2upcGallery": user2upcIndex2upcGallery,
        "demoGallery": [["https://i.imgur.com/llPpFm0.jpeg"]],
        "fullCatalog": full_catalog,
        "upcGallery": upc_gallery
    }

    state = {
        "dialogVisible": False,
        "user2selectedUpc": user2selectedUpc,
        "selectedKeywords": [],
        "searching": False,
        "perPage": 10,
        "pageSizes": [5, 10, 20],
        "user2selectedRowData": user2selectedRowData
    }

    # # start event after successful service run
    # events = [
    #     {
    #         "state": {},
    #         "context": {},
    #         "command": "calculate"
    #     }
    # ]

    # Run application service
    my_app.run(data=data, state=state)


if __name__ == "__main__":
    sly.main_wrapper("main", main)
