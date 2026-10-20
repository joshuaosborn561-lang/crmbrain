from crmbrain import hubspot as hs_mod
from crmbrain.hubspot import CONTACT_PROPS, DEAL_PROPS, HubSpot


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def test_boolean_props_have_true_false_options():
    for prop in CONTACT_PROPS + DEAL_PROPS:
        if prop.get("fieldType") == "booleancheckbox":
            vals = sorted(o["value"] for o in prop["options"])
            assert vals == ["false", "true"], prop["name"]


def _hs(post_resp):
    hs = HubSpot.__new__(HubSpot)
    posts = []

    def fake_request(method, path, **kw):
        if method == "GET":
            return _Resp(404)
        posts.append(kw.get("json"))
        return post_resp

    hs._request = fake_request
    return hs, posts


def test_already_exists_is_ok():
    hs, posts = _hs(_Resp(409, '{"message":"Property named crmbrain_bot already exists"}'))
    hs.ensure_properties()
    assert posts


def test_bool_prop_posted_with_options():
    hs, posts = _hs(_Resp(201))
    hs.ensure_properties()
    bot = [p for p in posts if p["name"] == "crmbrain_bot"][0]
    assert bot["options"][0]["value"] == "true"


def test_other_errors_still_raise():
    hs, _ = _hs(_Resp(400, "bad"))
    try:
        hs.ensure_properties()
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError")
