# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

"""
Let's test that Steve's as awesome as we think he is.
"""
import json
import shutil
from json import dumps, load

import pytest
from celery.canvas import Signature
from flexmock import flexmock
from github import Github
from ogr.services.github import GithubProject
from ogr.services.pagure import PagureProject

from packit.api import PackitAPI
from packit.config import JobConfigTriggerType
from packit.distgit import DistGit
from packit.local_project import LocalProject
from packit_service.config import ServiceConfig
from packit_service.constants import TASK_ACCEPTED
from packit_service.models import (
    JobTriggerModelType,
    PipelineModel,
    ProjectReleaseModel,
    SyncReleaseModel,
    SyncReleaseStatus,
    SyncReleaseTargetModel,
    SyncReleaseTargetStatus,
    SyncReleaseJobType,
)
from packit_service.service.db_triggers import AddReleaseDbTrigger
from packit_service.service.urls import get_propose_downstream_info_url
from packit_service.worker.allowlist import Allowlist
from packit_service.worker.helpers.sync_release.propose_downstream import (
    ProposeDownstreamJobHelper,
)
from packit_service.worker.jobs import SteveJobs
from packit_service.worker.monitoring import Pushgateway
from packit_service.worker.reporting import BaseCommitStatus
from packit_service.worker.tasks import run_propose_downstream_handler
from tests.spellbook import DATA_DIR, first_dict_value, get_parameters_from_results

EVENT = {
    "action": "published",
    "release": {"tag_name": "1.2.3"},
    "repository": {
        "name": "bar",
        "html_url": "https://github.com/the-namespace/the-repo",
        "owner": {"login": "foo"},
    },
}


@pytest.mark.parametrize(
    "event,private,enabled_private_namespaces,success",
    (
        (EVENT, False, set(), True),
        (EVENT, True, {"github.com/the-namespace"}, True),
        (EVENT, True, set(), False),
    ),
)
def test_process_message(event, private, enabled_private_namespaces, success):
    packit_yaml = {
        "specfile_path": "bar.spec",
        "synced_files": [],
        "jobs": [{"trigger": "release", "job": "propose_downstream"}],
    }
    flexmock(Github, get_repo=lambda full_name_or_id: None)
    gh_project = flexmock(
        GithubProject,
        get_file_content=lambda path, ref: dumps(packit_yaml),
        full_repo_name="the-namespace/the-repo",
        get_files=lambda ref, filter_regex: [],
        get_sha_from_tag=lambda tag_name: "12345",
        get_web_url=lambda: "https://github.com/the-namespace/the-repo",
        is_private=lambda: private,
    )
    gh_project.default_branch = "main"

    lp = flexmock(LocalProject, refresh_the_arguments=lambda: None)
    lp.git_project = gh_project
    lp.working_dir = ""
    flexmock(DistGit).should_receive("local_project").and_return(lp)
    # reset of the upstream repo
    flexmock(LocalProject).should_receive("git_repo").and_return(
        flexmock(
            head=flexmock()
            .should_receive("reset")
            .with_args("HEAD", index=True, working_tree=True)
            .times(1 if success else 0)
            .mock(),
            git=flexmock(clear_cache=lambda: None),
        )
    )

    ServiceConfig().get_service_config().enabled_private_namespaces = (
        enabled_private_namespaces
    )
    flexmock(PagureProject).should_receive("_call_project_api").and_return(
        {"default": "main"}
    )

    run_model = flexmock(PipelineModel)
    trigger = flexmock(
        job_trigger_model_type=JobTriggerModelType.release,
        id=12,
        job_config_trigger_type=JobConfigTriggerType.release,
    )
    flexmock(ProjectReleaseModel).should_receive("get_or_create").with_args(
        tag_name="1.2.3",
        namespace="the-namespace",
        repo_name="the-repo",
        project_url="https://github.com/the-namespace/the-repo",
        commit_hash="12345",
    ).and_return(trigger).times(1 if success else 0)
    propose_downstream_model = flexmock(sync_release_targets=[])
    flexmock(SyncReleaseModel).should_receive("create_with_new_run").with_args(
        status=SyncReleaseStatus.running,
        trigger_model=trigger,
        job_type=SyncReleaseJobType.propose_downstream,
    ).and_return(propose_downstream_model, run_model).times(1 if success else 0)

    model = flexmock(status="queued", id=1234, branch="main")
    flexmock(SyncReleaseTargetModel).should_receive("create").with_args(
        status=SyncReleaseTargetStatus.queued, branch="main"
    ).and_return(model).times(1 if success else 0)
    flexmock(model).should_receive("set_downstream_pr_url").with_args(
        downstream_pr_url="some_url"
    ).times(1 if success else 0)
    flexmock(model).should_receive("set_status").with_args(
        status=SyncReleaseTargetStatus.running
    ).times(1 if success else 0)
    flexmock(model).should_receive("set_start_time").times(1 if success else 0)
    flexmock(model).should_receive("set_finished_time").times(1 if success else 0)
    flexmock(model).should_receive("set_logs").times(1 if success else 0)
    flexmock(model).should_receive("set_status").with_args(
        status=SyncReleaseTargetStatus.submitted
    ).times(1 if success else 0)
    flexmock(propose_downstream_model).should_receive("set_status").with_args(
        status=SyncReleaseStatus.finished
    ).times(1 if success else 0)

    flexmock(PackitAPI).should_receive("sync_release").with_args(
        dist_git_branch="main", tag="1.2.3", create_pr=True
    ).and_return(flexmock(url="some_url")).times(1 if success else 0)
    flexmock(shutil).should_receive("rmtree").with_args("")

    flexmock(AddReleaseDbTrigger).should_receive("db_trigger").and_return(
        flexmock(
            job_config_trigger_type=JobConfigTriggerType.release,
            id=1,
            job_trigger_model_type=JobConfigTriggerType.release,
        )
    )
    flexmock(Allowlist, check_and_report=True)
    flexmock(Signature).should_receive("apply_async").times(1 if success else 0)

    flexmock(ProposeDownstreamJobHelper).should_receive(
        "report_status_to_all"
    ).with_args(
        description=TASK_ACCEPTED,
        state=BaseCommitStatus.pending,
        url="",
        markdown_content=None,
    ).times(
        1 if success else 0
    )
    flexmock(Pushgateway).should_receive("push").times(2 if success else 0)

    url = get_propose_downstream_info_url(model.id)

    flexmock(ProposeDownstreamJobHelper).should_receive(
        "report_status_for_branch"
    ).with_args(
        branch="main",
        description="Starting propose downstream...",
        state=BaseCommitStatus.running,
        url=url,
    ).times(
        1 if success else 0
    )
    flexmock(ProposeDownstreamJobHelper).should_receive(
        "report_status_for_branch"
    ).with_args(
        branch="main",
        description="Propose downstream finished successfully.",
        state=BaseCommitStatus.success,
        url=url,
    ).times(
        1 if success else 0
    )

    processing_results = SteveJobs().process_message(event)
    if not success:
        assert processing_results == []
        return

    event_dict, job, job_config, package_config = get_parameters_from_results(
        processing_results
    )
    assert json.dumps(event_dict)

    results = run_propose_downstream_handler(
        package_config=package_config,
        event=event_dict,
        job_config=job_config,
    )
    assert "propose_downstream" in next(iter(results["job"]))
    assert first_dict_value(results["job"])["success"]


@pytest.fixture()
def github_push():
    with open(DATA_DIR / "webhooks" / "github" / "push.json") as outfile:
        return load(outfile)


def test_ignore_delete_branch(github_push):
    flexmock(
        GithubProject,
        is_private=lambda: False,
    )

    processing_results = SteveJobs().process_message(github_push)

    assert processing_results == []
