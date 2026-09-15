#!/usr/bin/env python3
"""Validate agent-interface records against doc/agent-v1.schema.json.

A small, dependency-free JSON Schema (draft 2020-12) subset validator covering
exactly the keywords the agent schema uses, plus the positive and negative
vectors that pin the grammar.  Standard library only.

Usage:
    python3 schema_check.py                 # run the built-in vectors
    python3 schema_check.py --stdin         # also validate JSON lines on stdin
                                            # (used for real encoder output)

Byte-length validation is deliberately a separate layer, as the schema states:
maxLength counts characters, so the vectors below also exercise byte budgets.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA = os.path.join(ROOT, "doc/agent-v1.schema.json")

FAILURES = []


def deref(schema, root):
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/"), ref
        node = root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        schema = node
        seen += 1
        assert seen < 20
    return schema


def type_ok(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise AssertionError("unknown type " + name)


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def validate(value, schema, root, path, errs):
    schema = deref(schema, root)
    if schema is True or schema == {}:
        return

    if "type" in schema:
        names = schema["type"]
        names = names if isinstance(names, list) else [names]
        if not any(type_ok(value, n) for n in names):
            errs.append("%s: type %s not in %s" % (path, json_type(value), names))
            return

    if "const" in schema and value != schema["const"]:
        errs.append("%s: not const %r" % (path, schema["const"]))
        return

    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r not in enum" % (path, value))
        return

    if "not" in schema:
        sub = []
        validate(value, schema["not"], root, path, sub)
        if not sub:
            errs.append("%s: matched a forbidden subschema" % path)
            return

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s: %r < minimum %r" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("%s: %r > maximum %r" % (path, value, schema["maximum"]))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append("%s: too short" % path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append("%s: too long" % path)
        if "pattern" in schema:
            import re

            if not re.search(schema["pattern"], value):
                errs.append("%s: pattern mismatch" % path)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append("%s: too few items" % path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s: too many items" % path)
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(seen)) != len(seen):
                errs.append("%s: duplicate items" % path)
        prefix = schema.get("prefixItems")
        if prefix:
            for i, sub in enumerate(prefix):
                if i < len(value):
                    validate(value[i], sub, root, "%s[%d]" % (path, i), errs)
        items = schema.get("items")
        if items:
            start = len(prefix) if prefix else 0
            for i in range(start, len(value)):
                validate(value[i], items, root, "%s[%d]" % (path, i), errs)

    if isinstance(value, dict):
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errs.append("%s: too few properties" % path)
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errs.append("%s: too many properties" % path)
        for key in schema.get("required", []):
            if key not in value:
                errs.append("%s: missing required %r" % (path, key))
        props = schema.get("properties", {})
        patprops = schema.get("patternProperties", {})
        for key, sub in value.items():
            here = "%s.%s" % (path, key)
            if key in props:
                validate(sub, props[key], root, here, errs)
                continue
            matched = False
            for pat, psub in patprops.items():
                import re

                if re.search(pat, key):
                    validate(sub, psub, root, here, errs)
                    matched = True
            if matched:
                continue
            if schema.get("additionalProperties") is False:
                errs.append("%s: unexpected property %r" % (path, key))
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(sub, schema["additionalProperties"], root, here, errs)
        if "propertyNames" in schema:
            for key in value:
                validate(key, schema["propertyNames"], root, path, errs)

    if "allOf" in schema:
        for sub in schema["allOf"]:
            validate(value, sub, root, path, errs)

    if "oneOf" in schema:
        hits = 0
        for sub in schema["oneOf"]:
            sub_errs = []
            validate(value, sub, root, path, sub_errs)
            if not sub_errs:
                hits += 1
        if hits != 1:
            errs.append("%s: oneOf matched %d branches" % (path, hits))

    if "anyOf" in schema:
        hits = 0
        for sub in schema["anyOf"]:
            sub_errs = []
            validate(value, sub, root, path, sub_errs)
            if not sub_errs:
                hits += 1
        if hits == 0:
            errs.append("%s: anyOf matched no branch" % path)

    if "if" in schema:
        sub = []
        validate(value, schema["if"], root, path, sub)
        if not sub and "then" in schema:
            validate(value, schema["then"], root, path, errs)
        elif sub and "else" in schema:
            validate(value, schema["else"], root, path, errs)


def check(rec, schema, label, expect_ok=True):
    errs = []
    validate(rec, schema, schema, "$", errs)
    ok = not errs
    if ok != expect_ok:
        FAILURES.append("%s: expected %s, got %s %s"
                        % (label, "valid" if expect_ok else "invalid",
                           "valid" if ok else "invalid",
                           "" if ok else errs[:3]))


MAP_TRIPLES = [[1, 0, 1], [2, 0, 2]]
PAL = [[0, " ", "none", 0, "none"], [1, ".", "gray", 0, "none"],
       [2, "@", "white", 32, "none"]]


def obs(**kw):
    base = {
        "v": 1, "ch": "player", "type": "obs", "d": 2, "seq": 1, "base": None,
        "s": {"time": {"text": "42", "color": "none", "style": 0}},
        "cond": [{"text": "Blind", "color": "none", "style": 0}],
        "pal": PAL, "map": MAP_TRIPLES, "cur": [1, 0],
        "msg": [{"e": 1, "text": "hello", "style": 0}],
        "hist": [], "windows": [], "need": {"id": 1, "kind": "command"},
    }
    base.update(kw)
    return base


def chunk_rec(rid, i, last, parts, d=1):
    return {"v": 1, "ch": "control", "type": "chunk", "d": d, "rid": rid,
            "i": i, "last": last, "parts": parts}


def main(argv):
    schema = json.load(open(SCHEMA))

    positive = [
        ("hello", {"v": 1, "ch": "control", "type": "hello", "d": 1,
                   "profile": "normal-ascii-color-v1", "policy": "llm-final-v1",
                   "caps": ["snapshot", "menu", "paging"],
                   "coord": "engine-map", "size": [80, 21], "x0": 1, "y0": 0,
                   "limits": {"line": 65536, "page_bytes": 16384,
                              "page_rows": 128, "count": 2147483647}}),
        ("obs full, need null", obs(need=None)),
        ("obs with a need", obs()),
        ("obs with an empty collection set",
         obs(s={}, cond=[], pal=[[0, " ", "none", 0, "none"]], map=[],
             cur=None, msg=[], hist=[], windows=[])),
        ("obs with a menu window",
         obs(windows=[{"w": "w1", "kind": "menu", "title": "eat?",
                       "mode": "any", "content": "c1", "pages": 2}])),
        ("obs with a text window",
         obs(windows=[{"w": "w2", "kind": "text", "title": "Inventory",
                       "content": "c2", "pages": 1}])),
        ("act key", {"v": 1, "type": "act", "id": 1,
                     "action": {"key": 104}}),
        ("act text", {"v": 1, "type": "act", "id": 1,
                      "action": {"text": "ab"}}),
        ("act position", {"v": 1, "type": "act", "seq": 2, "id": 1,
                          "action": {"position": [12, 8], "mod": 0}}),
        ("act yn with count", {"v": 1, "type": "act", "id": 1,
                               "action": {"yn": 121, "count": 3}}),
        ("act menu commit", {"v": 1, "type": "act", "id": 1,
                             "action": {"menu": "m1",
                                        "commit": [[2, -1], [5, 3]]}}),
        ("act cancel", {"v": 1, "type": "act", "id": 1,
                        "action": {"cancel": True}}),
        ("act ack", {"v": 1, "type": "act", "id": 1, "action": {"ack": True}}),
        ("closed", {"v": 1, "ch": "control", "type": "closed"}),
        ("ack_seq", {"v": 1, "type": "ack_seq", "seq": 1}),
        ("ack_chunk", {"v": 1, "type": "ack_chunk", "rid": 1, "i": 0}),
        ("get_page", {"v": 1, "type": "get_page", "id": 1, "content": "c1",
                      "page": 0}),
        ("invalid", {"v": 1, "ch": "control", "type": "invalid", "d": 3,
                     "code": "stale"}),
        ("menu need at the page maximum",
         obs(need={"id": 1, "kind": "menu", "menu": "m1", "mode": "any",
                   "content": "c1", "pages": 65535})),
        ("ack need at the page maximum",
         obs(need={"id": 1, "kind": "ack", "content": "c1",
                   "pages": 65535})),
    ]
    for label, rec in positive:
        check(rec, schema, "positive " + label, True)

    # chunk records, including the long-text t part
    positives = [
        ("chunk header parts",
         chunk_rec(1, 0, False, [{"p": "h", "k": "v", "val": 1},
                                 {"p": "h", "k": "seq", "val": 1},
                                 {"p": "h", "k": "base", "val": None}])),
        ("chunk map part",
         chunk_rec(1, 0, True, [{"p": "map", "val": [1, 0, 1]}])),
        ("chunk need null",
         chunk_rec(1, 0, True, [{"p": "need", "val": None}])),
        ("chunk t text slice",
         chunk_rec(1, 1, True, [{"p": "t", "k": "msg", "e": 7, "f": "text",
                                 "offset": 0, "text": "abc", "last": True}])),
        ("chunk t title slice",
         chunk_rec(1, 1, True, [{"p": "t", "k": "win", "w": "w2",
                                 "f": "title", "offset": 4, "text": "zz",
                                 "last": False}])),
        ("page menu rows",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 0, "pages": 1,
          "rows": [{"r": 1, "text": "a", "selectable": True, "key": None,
                    "group": None, "initial": -1, "style": 0,
                    "color": "gray",
                    "icon": [")", "gray", 0, "none"]}]}),
        ("page record at the last legal index",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 65534, "pages": 65535, "rows": []}),
        ("page text rows",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c2",
          "page": 0, "pages": 1, "rows": [{"text": "line", "style": 0}]}),
    ]
    for label, rec in positives:
        check(rec, schema, "positive " + label, True)

    # negative vectors: every one must be rejected
    negatives = [
        ("closed with a counter", {"v": 1, "ch": "control", "type": "closed",
                                   "d": 1}),
        ("closed with a reason", {"v": 1, "ch": "control", "type": "closed",
                                  "reason": "x"}),
        ("obs missing a container", {k: v for k, v in obs().items()
                                     if k != "msg"}),
        ("obs with an unexpected key",
         dict(obs(), extra=1)),
        ("obs with a bad seq", obs(seq=0)),
        ("obs with a counter past 2^53-1", obs(d=9007199254740992)),
        ("obs with an unknown status field",
         obs(s={"hungerpoints": {"text": "1", "color": "none", "style": 0}})),
        ("obs with a bad colour",
         obs(pal=[[0, " ", "chartreuse", 0, "none"]])),
        ("obs with a non-ASCII char",
         obs(pal=[[0, "\u00e9", "none", 0, "none"]])),
        ("obs with x=0", obs(map=[[0, 0, 1]])),
        ("obs with y=21", obs(map=[[1, 21, 1]])),
        ("menu window without a mode",
         obs(windows=[{"w": "w1", "kind": "menu", "title": "t",
                       "content": "c1", "pages": 1}])),
        ("act hinting a raw key", {"v": 1, "type": "act", "id": 1,
                                   "action": {"keys": "hh"}}),
        ("act selectall", {"v": 1, "type": "act", "id": 1,
                           "action": {"selectall": True}}),
        ("act position x=0", {"v": 1, "type": "act", "id": 1,
                              "action": {"position": [0, 3], "mod": 0}}),
        ("act position mod 1", {"v": 1, "type": "act", "id": 1,
                                "action": {"position": [1, 3], "mod": 1}}),
        ("act commit count 0", {"v": 1, "type": "act", "id": 1,
                                "action": {"menu": "m1", "commit": [[1, 0]]}}),
        ("act commit count -2", {"v": 1, "type": "act", "id": 1,
                                 "action": {"menu": "m1",
                                            "commit": [[1, -2]]}}),
        ("act key 0", {"v": 1, "type": "act", "id": 1,
                       "action": {"key": 0}}),
        ("act two shapes", {"v": 1, "type": "act", "id": 1,
                            "action": {"key": 1, "cancel": True}}),
        ("act bad version", {"v": 2, "type": "act", "id": 1,
                             "action": {"key": 1}}),
        ("aux with an extra key", {"v": 1, "type": "ack_seq", "seq": 1,
                                   "junk": 2}),
        ("chunk header part carrying d",
         chunk_rec(1, 0, True, [{"p": "h", "k": "d", "val": 1}])),
        ("chunk part with an unknown path",
         chunk_rec(1, 0, True, [{"p": "nope", "val": 1}])),
        ("t part naming neither e nor w",
         chunk_rec(1, 1, True, [{"p": "t", "k": "msg", "f": "text",
                                 "offset": 0, "text": "a", "last": True}])),
        ("t part for a window with f=text",
         chunk_rec(1, 1, True, [{"p": "t", "k": "win", "w": "w1",
                                 "f": "text", "offset": 0, "text": "a",
                                 "last": True}])),
        ("page row with initial 0",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 0, "pages": 1,
          "rows": [{"r": 1, "text": "a", "selectable": True, "key": None,
                    "group": None, "initial": 0, "style": 0, "color": "gray",
                    "icon": None}]}),
        ("page index one past the maximum",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 65535, "pages": 65535, "rows": []}),
        ("page count one past the maximum",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 0, "pages": 65536, "rows": []}),
        ("menu need one page past the maximum",
         obs(need={"id": 1, "kind": "menu", "menu": "m1", "mode": "any",
                   "content": "c1", "pages": 65536})),
        ("text window carrying a mode",
         {"v": 1, "ch": "control", "type": "page", "d": 4, "content": "c1",
          "page": 0, "pages": 1,
          "rows": [{"r": 1, "text": "a", "key": None, "group": None,
                    "initial": -1, "style": 0, "color": "gray",
                    "icon": None}]}),
    ]
    for label, rec in negatives:
        check(rec, schema, "negative " + label, False)

    # UTF-8 byte-length is a separate mandatory layer
    long_text = {"v": 1, "type": "act", "id": 1,
                 "action": {"text": "\u00e9" * 128}}
    errs = []
    validate(long_text, schema, schema, "$", errs)
    if errs:
        FAILURES.append("byte-layer vector: JSON schema should accept a "
                        "128-character text (%d bytes) but rejected it" % 256)
    if len(long_text["action"]["text"].encode()) != 256:
        FAILURES.append("byte-layer vector: expected 256 bytes")

    if "--stdin" in argv:
        n = 0
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            errs = []
            validate(rec, schema, schema, "$", errs)
            n += 1
            if errs:
                FAILURES.append("encoder output line %d invalid: %s"
                                % (n, errs[:3]))
        print("encoder output lines validated: %d" % n)

    if FAILURES:
        print("schema_check: %d failure(s)" % len(FAILURES))
        for f in FAILURES:
            print("  " + f)
        return 1
    print("schema_check: %d positive, %d negative vectors ok"
          % (len(positive) + len(positives), len(negatives)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
