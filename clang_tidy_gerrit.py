#!/usr/bin/env python2
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import argparse
import collections
import compile_flags
import json
import logging
import multiprocessing
from multiprocessing.pool import ThreadPool
import os
import re
import subprocess
import sys
import unittest
import tempfile

from requests.exceptions import RequestException
from pygerrit2 import GerritRestAPI
from pygerrit2 import HTTPDigestAuthFromNetrc, HTTPBasicAuthFromNetrc
from pygerrit2 import HTTPBasicAuth, HTTPDigestAuth

from kudu_util import init_logging

#ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.getcwd()

#CLANG_TIDY_DIFF = os.path.join(
#    ROOT, "thirdparty/installed/uninstrumented/share/clang/clang-tidy-diff.py")
#CLANG_TIDY = os.path.join(
#    ROOT, "thirdparty/clang-toolchain/bin/clang-tidy")
#GERRIT_USER = 'tidybot'
#GERRIT_PASSWORD = os.getenv('TIDYBOT_PASSWORD')
#GERRIT_URL = "https://gerrit.cloudera.org"

CLANG_TIDY_DIFF = os.path.join(
    "", "/home/user/clang/src/llvm-7.0.1.src/tools/clang/tools/extra/clang-tidy/tool/clang-tidy-diff.py")

CLANG_TIDY = os.path.join(
    "", "/home/user/clang/src/build/bin/clang-tidy")

GERRIT_USER = 'admin'
GERRIT_PASSWORD = 'QOVfo5eISRerNelQr2meopO2zzMQh0IhSDwQq5KkYg'
GERRIT_URL = "http://192.168.56.101:8080"



def run_tidy(sha="HEAD", is_rev_range=False):
    diff_cmdline = ["git", "diff" if is_rev_range else "show", sha]

    logging.info("diff_cmdline=%s", diff_cmdline + ["--name-only", "--pretty=format:"])
    # Figure out which paths changed in the given diff.
    changed_paths = subprocess.check_output(diff_cmdline + ["--name-only", "--pretty=format:"]).splitlines()
    changed_paths = [p for p in changed_paths if p]
    logging.info("change_paths=%s", changed_paths)

    # Produce a separate diff for each file and run clang-tidy-diff on it
    # in parallel.
    def tidy_on_path(path):
        #patch_file = tempfile.NamedTemporaryFile()
        patch_file = tempfile.NamedTemporaryFile(prefix='review_robot.',delete=False)
        cmd = diff_cmdline + [
            "-U0",
            "--src-prefix=%s/" % ROOT,
            "--dst-prefix=%s/" % ROOT,
            "--",
            path]
        logging.info("path=%s", path)
        logging.info("cmd=%s", cmd)
        logging.info("patch_file=%s",patch_file.name)
        subprocess.check_call(cmd, stdout=patch_file, cwd=ROOT)

        cmdline = [CLANG_TIDY_DIFF,
                   "-clang-tidy-binary", CLANG_TIDY,
                   "-p0",
                   "--",
                   "-DCLANG_TIDY"] + compile_flags.get_flags()

        logging.info("cmdline=%s", cmdline)

        return subprocess.check_output(
            cmdline,
            stdin=file(patch_file.name),
            cwd=ROOT)

 
    pool = ThreadPool(multiprocessing.cpu_count())
    pool = ThreadPool(1)
    try:
        return "".join(pool.imap(tidy_on_path, changed_paths))
    except KeyboardInterrupt as ki:
        sys.exit(1)
    finally:
        pool.terminate()
        pool.join()


def split_warnings(clang_output):
    accumulated = ""
    for l in clang_output.splitlines():
        if l == "":
            continue
        if l.startswith(ROOT) and re.search(r'(warning|error): ', l):
            if accumulated:
                yield accumulated
            accumulated = ""
        accumulated += l + "\n"
    if accumulated:
        yield accumulated


def parse_clang_output(clang_output):
    ret = []
    for w in split_warnings(clang_output):
        m = re.match(r"^(.+?):(\d+):\d+: ((?:warning|error): .+)$", w, re.MULTILINE | re.DOTALL)
        if not m:
            raise Exception("bad warning: " + w)
        path, line, warning = m.groups()
        ret.append(dict(
            path=os.path.relpath(path, ROOT),
            line=int(line),
            warning=warning.strip()))
    return ret

def create_gerrit_json_obj(parsed_warnings):
    comments = collections.defaultdict(lambda: [])
    for warning in parsed_warnings:
        comments[warning['path']].append({
            'line': warning['line'],
            'message': warning['warning']
            })
    return {"comments": comments,
            "notify": "OWNER",
            "omit_duplicate_comments": "false"}


def get_gerrit_revision_url(git_ref):
    sha = subprocess.check_output(["git", "rev-parse", git_ref]).strip()

    commit_msg = subprocess.check_output(
        ["git", "show", sha])
    matches = re.findall(r'^\s+Change-Id: (I.+)$', commit_msg, re.MULTILINE)
    if not matches:
        raise Exception("Could not find gerrit Change-Id for commit %s" % sha)
    if len(matches) != 1:
        raise Exception("Found multiple gerrit Change-Ids for commit %s" % sha)
    change_id = matches[0]
    return "%s/a/changes/%s/revisions/%s" % (GERRIT_URL, change_id, sha)

def get_gerrit_sha(git_ref):
    sha = subprocess.check_output(["git", "rev-parse", git_ref]).strip()
    return sha


def get_gerrit_change_id(git_ref):
    sha = get_gerrit_sha(git_ref)

    commit_msg = subprocess.check_output(
        ["git", "show", sha])
    matches = re.findall(r'^\s+Change-Id: (I.+)$', commit_msg, re.MULTILINE)
    if not matches:
        raise Exception("Could not find gerrit Change-Id for commit %s" % sha)
    if len(matches) != 1:
        raise Exception("Found multiple gerrit Change-Ids for commit %s" % sha)
    change_id = matches[0]
    return change_id



def rest_get_gerrit_change_number(rest,change_id,project,branch):

    change_number = 0

    try:
        query = ["change:%s" % change_id]

	if len(project)>0:
             query += ["project:%s" % project]
	if len(branch)>0:
             query += ["branch:%s" % branch]

        query += ["limit:30"]

	endpoint = "/changes/?q=%s" % "%20".join(query)

	logging.info("endpoint=%s", endpoint)

        changes = rest.get(endpoint)
        logging.info("%d changes", len(changes))

        for change in changes:
            logging.info(change['change_id'])
            logging.info(change['id'])
            logging.info(change)
	    change_number = change['_number']

    except RequestException as err:
        logging.error("Error: %s", str(err))

    return change_number


def rest_compose_gerrit_change_id(project,change_id):
    import urllib
    return  urllib.quote(project, safe='') + "~" + change_id


def rest_post_gerrit_comments(rest, rest_change_id, revision_id, data):

    # the /a/ will be appneded by pygerryt2
    endpoint = "changes/%s/revisions/%s/review" % (rest_change_id, revision_id)

    logging.info("endpoint=%s", endpoint)

    r = rest.post(endpoint,
                      data=json.dumps(gerrit_json_obj),
                      headers={'Content-Type': 'application/json'})
    print "Response:"
    print r


def rest_post_gerrit(rest,endpoint, data):

    r = rest.post(endpoint,
                      data=json.dumps(gerrit_json_obj),
                      headers={'Content-Type': 'application/json'})
    print "Response:"
    print r




def post_comments(revision_url_base, gerrit_json_obj):
    import requests

    logging.info("url=%s",revision_url_base + "/review")	
    logging.info("auth=%s",GERRIT_USER +" "+ GERRIT_PASSWORD);	

    r = requests.post(revision_url_base + "/review",
                      auth=(GERRIT_USER, GERRIT_PASSWORD),
                      data=json.dumps(gerrit_json_obj),
                      headers={'Content-Type': 'application/json'})
    print "Response:"
    print r.headers
    print r.status_code
    print r.text


class TestClangTidyGerrit(unittest.TestCase):
    TEST_INPUT = \
"""
/home/todd/git/kudu/src/kudu/integration-tests/tablet_history_gc-itest.cc:579:55: warning: some warning [warning-name]
   some example line of code

/home/todd/git/kudu/foo/../src/kudu/blah.cc:123:55: error: some error
   blah blah
"""

    def test_parse_clang_output(self):
        global ROOT
        save_root = ROOT
        try:
            ROOT = "/home/todd/git/kudu"
            parsed = parse_clang_output(self.TEST_INPUT)
        finally:
            ROOT = save_root
        self.assertEqual(2, len(parsed))

        self.assertEqual("src/kudu/integration-tests/tablet_history_gc-itest.cc", parsed[0]['path'])
        self.assertEqual(579, parsed[0]['line'])
        self.assertEqual("warning: some warning [warning-name]\n" +
                         "   some example line of code",
                         parsed[0]['warning'])

        self.assertEqual("src/kudu/blah.cc", parsed[1]['path'])



if __name__ == "__main__":
    # Basic setup and argument parsing.
    init_logging()
    parser = argparse.ArgumentParser(
        description="Run clang-tidy on a patch, optionally posting warnings as comments to gerrit")
    parser.add_argument("-n", "--no-gerrit", action="store_true",
                        help="Whether to run locally i.e. (no interaction with gerrit)")
    parser.add_argument("--rev-range", action="store_true",
                        default=False,
                        help="Whether the revision specifies the 'rev..' range")
    parser.add_argument('rev', help="The git revision (or range of revisions) to process")
    args = parser.parse_args()

    if args.rev_range and not args.no_gerrit:
        print >>sys.stderr, "--rev-range works only with --no-gerrit"
        sys.exit(1)

    # Run clang-tidy and parse the output.
    clang_output = run_tidy(args.rev, args.rev_range)
    logging.info("Clang output")
    logging.info(clang_output)
    if args.no_gerrit:
        print >>sys.stderr, "Skipping gerrit"
        sys.exit(0)
    logging.info("=" * 80)
    parsed = parse_clang_output(clang_output)
    if not parsed:
        print >>sys.stderr, "No warnings"
        sys.exit(0)
    print "Parsed clang warnings:"
    print json.dumps(parsed, indent=4)

    # Find the gerrit revision URL, if applicable.
    if not args.no_gerrit:
        revision_url = get_gerrit_revision_url(args.rev)
        change_id = get_gerrit_change_id(args.rev)
        sha = get_gerrit_sha(args.rev)
        print ("revision_url={0}".format(revision_url))
        print ("change_id={0}".format(change_id))
        print ("sha={0}".format(sha))

    project ="main/sub1"
    branch = "main/sub1/master"



    auth = HTTPBasicAuth(GERRIT_USER, GERRIT_PASSWORD)
    rest = GerritRestAPI(GERRIT_URL, auth=auth)
    change_number = str(rest_get_gerrit_change_number(rest,change_id,project,branch))

    print ("change_number={0}".format(change_number))

    # Post the output as comments to the gerrit URL.
    gerrit_json_obj = create_gerrit_json_obj(parsed)
    print "Will post to gerrit:"
    print json.dumps(gerrit_json_obj, indent=4)


    rest_change_id = rest_compose_gerrit_change_id(project,change_number)

    print ("project={0} branch={1} rest_change_id={2}".format(project, branch, rest_change_id))

    rest_post_gerrit_comments(rest, rest_change_id,"current", gerrit_json_obj)




    #post_comments(revision_url, gerrit_json_obj)
