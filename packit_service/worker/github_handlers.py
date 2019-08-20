# MIT License
#
# Copyright (c) 2018-2019 Red Hat, Inc.

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
This file defines classes for job handlers specific for Github hooks
"""
import json
import logging
import uuid
from pathlib import Path
from typing import Union, Any, Optional

import requests
from ogr import GithubService
from ogr.abstract import GitProject
from ogr.utils import RequestResponse
from packit.api import PackitAPI
from packit.config import (
    JobConfig,
    JobTriggerType,
    JobType,
    PackageConfig,
    get_package_config_from_repo,
)
from packit.exceptions import FailedCreateSRPM
from packit.local_project import LocalProject
from sandcastle import SandcastleCommandFailed, SandcastleTimeoutReached

from packit_service.config import Config, Deployment
from packit_service.constants import TESTING_FARM_TRIGGER_URL
from packit_service.service.events import (
    PullRequestEvent,
    InstallationEvent,
    ReleaseEvent,
)
from packit_service.service.models import Installation, CoprBuild
from packit_service.worker.handler import (
    JobHandler,
    HandlerResults,
    add_to_mapping,
    BuildStatusReporter,
    PRCheckName,
)
from packit_service.worker.whitelist import Whitelist

logger = logging.getLogger(__name__)


class AbstractGithubJobHandler(JobHandler):
    def __get_private_key(self):
        if self.config.github_app_cert_path:
            return Path(self.config.github_app_cert_path).read_text()
        return None

    @property
    def github_service(self) -> GithubService:
        return GithubService(
            token=self.config.github_token,
            github_app_id=self.config.github_app_id,
            github_app_private_key=self.__get_private_key(),
        )


@add_to_mapping
class GithubPullRequestHandler(AbstractGithubJobHandler):
    name = JobType.check_downstream
    triggers = [JobTriggerType.pull_request]

    # https://developer.github.com/v3/activity/events/types/#events-api-payload-28

    def __init__(self, config: Config, job: JobConfig, pr_event: PullRequestEvent):
        super(GithubPullRequestHandler, self).__init__(config=config, job=job)
        self.pr_event = pr_event
        self.project: GitProject = self.github_service.get_project(
            repo=pr_event.base_repo_name, namespace=pr_event.base_repo_namespace
        )
        self.package_config: PackageConfig = get_package_config_from_repo(
            self.project, pr_event.base_ref
        )
        self.package_config.upstream_project_url = pr_event.https_url

    def run(self) -> HandlerResults:
        self.local_project = LocalProject(
            git_project=self.project, working_dir=self.config.command_handler_work_dir
        )

        self.api = PackitAPI(self.config, self.package_config, self.local_project)

        self.api.sync_pr(
            pr_id=self.pr_event.pr_id,
            dist_git_branch=self.job.metadata.get("dist-git-branch", "master"),
            # TODO: figure out top upstream commit for source-git here
        )
        return HandlerResults(success=True, details={})


@add_to_mapping
class GithubAppInstallationHandler(AbstractGithubJobHandler):
    name = JobType.add_to_whitelist
    triggers = [JobTriggerType.installation]

    # https://developer.github.com/v3/activity/events/types/#events-api-payload-28

    def __init__(
        self,
        config: Config,
        job: JobConfig,
        installation_event: Union[InstallationEvent, Any],
    ):
        super(GithubAppInstallationHandler, self).__init__(config=config, job=job)

        self.installation_event = installation_event
        self.project = self.github_service.get_project(
            repo="notifications", namespace="packit-service"
        )

    def run(self) -> HandlerResults:
        """
        Discover information about organization/user which wants to install packit on his repository
        Try to whitelist automatically if mapping from github username to FAS account can prove that
        user is a packager.
        :return: HandlerResults
        """

        # try to add user to whitelist
        whitelist = Whitelist()
        Installation.create(
            installation_id=self.installation_event.installation_id,
            event=self.installation_event,
        )
        if not whitelist.add_account(self.installation_event):
            # Create an issue in our repository, so we are notified when someone install the app
            self.project.create_issue(
                title=f"Account: {self.installation_event.account_login} needs to be approved.",
                body=(
                    f"Hi @{self.installation_event.account_login}, we need to approve you in "
                    "order to start using Packit-as-a-Service. Someone from our team will "
                    "get back to you shortly."
                ),
            )

            msg = f"Account: {self.installation_event.account_login} needs to be approved manually!"
            logger.info(msg)
            return HandlerResults(success=True, details={"msg": msg})
        return HandlerResults(
            success=True,
            details={
                "msg": f"Account {self.installation_event.account_login} whitelisted!"
            },
        )


@add_to_mapping
class GithubReleaseHandler(AbstractGithubJobHandler):
    name = JobType.propose_downstream
    triggers = [JobTriggerType.release]

    def __init__(self, config: Config, job: JobConfig, release_event: ReleaseEvent):
        super(GithubReleaseHandler, self).__init__(config=config, job=job)
        self.release_event = release_event

        self.project: GitProject = self.github_service.get_project(
            repo=release_event.repo_name, namespace=release_event.repo_namespace
        )
        self.package_config: PackageConfig = get_package_config_from_repo(
            self.project, release_event.tag_name
        )
        self.package_config.upstream_project_url = release_event.https_url

    def run(self) -> HandlerResults:
        """
        Sync the upstream release to dist-git as a pull request.
        """

        self.local_project = LocalProject(
            git_project=self.project, working_dir=self.config.command_handler_work_dir
        )

        self.api = PackitAPI(self.config, self.package_config, self.local_project)
        # create_pr is set to False.
        # Each upstream project decides
        # if creates PR or pushes directly into dist-git directly from packit.yaml file.
        self.api.sync_release(
            dist_git_branch=self.job.metadata.get("dist-git-branch", "master"),
            version=self.release_event.tag_name,
            create_pr=False,
        )

        return HandlerResults(success=True, details={})


@add_to_mapping
class GithubCoprBuildHandler(AbstractGithubJobHandler):
    name = JobType.copr_build
    triggers = [JobTriggerType.pull_request, JobTriggerType.release]

    def __init__(
        self,
        config: Config,
        job: JobConfig,
        event: Union[PullRequestEvent, ReleaseEvent],
    ):
        super(GithubCoprBuildHandler, self).__init__(config=config, job=job)

        self.event = event

        if isinstance(event, PullRequestEvent):
            repo_name = event.base_repo_name
            repo_namespace = event.base_repo_namespace
            base_ref = event.base_ref
        elif isinstance(event, ReleaseEvent):
            repo_name = event.repo_name
            repo_namespace = event.repo_namespace
            base_ref = event.tag_name

        self.project: GitProject = self.github_service.get_project(
            repo=repo_name, namespace=repo_namespace
        )
        self.package_config: PackageConfig = get_package_config_from_repo(
            self.project, base_ref
        )
        self.package_config.upstream_project_url = event.https_url

    def handle_pull_request(self):

        check_name = PRCheckName.get_build_check()

        if not self.job.metadata.get("targets"):
            logger.error(
                "'targets' value is required in packit config for copr_build job"
            )

        self.local_project = LocalProject(
            git_project=self.project,
            pr_id=self.event.pr_id,
            git_service=self.project.service,
            working_dir=self.config.command_handler_work_dir,
        )
        self.api = PackitAPI(self.config, self.package_config, self.local_project)

        # add suffix stg when using stg app
        stg = "-stg" if self.config.deployment == Deployment.stg else ""
        default_project_name = (
            f"{self.project.namespace}-{self.project.repo}-{self.event.pr_id}{stg}"
        )
        collaborators = self.project.who_can_merge_pr()
        project = self.job.metadata.get("project") or default_project_name
        owner = self.job.metadata.get("owner") or self.api.copr.config.get("username")
        chroots = self.job.metadata.get("targets")
        copr_build_model = CoprBuild.create(
            project=project, owner=owner, chroots=chroots
        )
        r = BuildStatusReporter(self.project, self.event.commit_sha, copr_build_model)
        if self.event.github_login not in collaborators:
            msg = "Only collaborators can trigger Packit-as-a-Service"
            r.set_status("failure", msg, PRCheckName.get_build_check())
            return HandlerResults(success=False, details={"msg": msg})
        try:
            r.report("pending", "RPM build has just started...", check_name=check_name)
            build_id, repo_url = self.api.run_copr_build(
                project=project, chroots=chroots, owner=owner
            )
        except SandcastleTimeoutReached:
            msg = "You have reached 10-minute timeout while creating the SRPM."
            self.project.pr_comment(self.event.pr_id, msg)
            msg = "Timeout reached while creating a SRPM."
            r.report("failure", msg, check_name=check_name)
            return HandlerResults(success=False, details={"msg": msg})
        except SandcastleCommandFailed as ex:
            max_log_size = 1024 * 16  # is 16KB enough?
            if len(ex.output) > max_log_size:
                output = "Earlier output was truncated\n\n" + ex.output[-max_log_size:]
            else:
                output = ex.output
            msg = (
                "There was an error while creating a SRPM.\n"
                "\nOutput:"
                "\n```\n"
                f"{output}"
                "\n```"
                f"\nReturn code: {ex.rc}"
            )
            self.project.pr_comment(self.event.pr_id, msg)
            msg = "Failed to create a SRPM."
            r.report("failure", msg, check_name=check_name)
            return HandlerResults(success=False, details={"msg": msg})
        except FailedCreateSRPM:
            msg = "Failed to create a SRPM."
            r.report("failure", msg, check_name=check_name)
            return HandlerResults(success=False, details={"msg": msg})

        except Exception as ex:
            logger.error(f"error while running a copr build: {ex}")
            msg = f"There was an error while running the build: {ex}"
            r.report("failure", msg, check_name=check_name)
            return HandlerResults(success=False, details={"msg": msg})

        copr_build_model.build_id = build_id
        copr_build_model.save()

        timeout_config = self.job.metadata.get("timeout")
        timeout = int(timeout_config) if timeout_config else 60 * 60 * 2
        build_state = self.api.watch_copr_build(build_id, timeout, report_func=r.report)
        if build_state == "succeeded":
            msg = (
                f"Congratulations! The build [has finished]({repo_url})"
                " successfully. :champagne:\n\n"
                "You can install the built RPMs by following these steps:\n\n"
                "* `sudo yum install -y dnf-plugins-core` on RHEL 8\n"
                "* `sudo dnf install -y dnf-plugins-core` on Fedora\n"
                f"* `dnf copr enable {owner}/{project}`\n"
                "* And now you can install the packages.\n"
                "\nPlease note that the RPMs should be used only in a testing environment."
            )
            self.project.pr_comment(self.event.pr_id, msg)

            # Testing farm is triggered just once copr build is finished as it uses copr builds
            # todo: utilize fedmsg for this.
            test_job_config = self.get_tests_for_build()
            if test_job_config:
                testing_farm_handler = GithubTestingFarmHandler(
                    self.config, test_job_config, self.event
                )
                testing_farm_handler.run()
            else:
                logger.debug("Testing farm not in the job config.")
            return HandlerResults(success=True, details={})

    def get_tests_for_build(self) -> Optional[JobConfig]:
        """
        Check if there are tests defined
        :return: JobConfig or None
        """
        for job in self.package_config.jobs:
            if job.job == JobType.tests:
                return job
        return None

    def run(self) -> HandlerResults:
        if self.event.trigger == JobTriggerType.pull_request:
            return self.handle_pull_request()
        # We do not support this workflow officially
        # elif self.triggered_by == JobTriggerType.release:
        #     self.handle_release()
        else:
            return HandlerResults(
                success=False,
                details={"msg": f"No handler for {str(self.event.trigger)}"},
            )


class GithubTestingFarmHandler(AbstractGithubJobHandler):
    """
    This class intentionally does not have a @add_to_mapping decorator as its
    trigger is finished copr build.
    """

    name = JobType.tests
    triggers = [JobTriggerType.pull_request]

    def __init__(self, config: Config, job: JobConfig, pr_event: PullRequestEvent):
        super(GithubTestingFarmHandler, self).__init__(config=config, job=job)
        self.pr_event = pr_event
        self.project: GitProject = self.github_service.get_project(
            repo=pr_event.base_repo_name, namespace=pr_event.base_repo_namespace
        )
        self.package_config: PackageConfig = get_package_config_from_repo(
            self.project, pr_event.base_ref
        )
        self.package_config.upstream_project_url = pr_event.https_url

        self.session = requests.session()
        adapter = requests.adapters.HTTPAdapter(max_retries=5)
        self.insecure = False
        self.session.mount("https://", adapter)
        self.header: dict = {"Content-Type": "application/json"}

    def send_testing_farm_request(
        self, url: str, method: str = None, params: dict = None, data=None
    ):
        method = method or "GET"
        try:
            response = self.get_raw_request(
                method=method, url=url, params=params, data=data
            )
        except requests.exceptions.ConnectionError as er:
            logger.error(er)
            raise Exception(f"Cannot connect to url: `{url}`.", er)
        return response

    def get_raw_request(
        self, url, method="GET", params=None, data=None, header=None
    ) -> RequestResponse:

        response = self.session.request(
            method=method,
            url=url,
            params=params,
            headers=header or self.header,
            data=data,
            verify=not self.insecure,
        )

        json_output = None
        try:
            json_output = response.json()
        except ValueError:
            logger.debug(response.text)

        return RequestResponse(
            status_code=response.status_code,
            ok=response.ok,
            content=response.content,
            json=json_output,
            reason=response.reason,
        )

    def run(self) -> HandlerResults:
        self.local_project = LocalProject(
            git_project=self.project, working_dir=self.config.command_handler_work_dir
        )

        r = BuildStatusReporter(self.project, self.pr_event.commit_sha)

        chroots = self.job.metadata.get("targets")
        for chroot in chroots:
            pipeline_id = str(uuid.uuid4())
            payload: dict = {
                "pipeline": {"id": pipeline_id},
                "api": {"token": self.config.testing_farm_secret},
            }

            stg = "-stg" if self.config.deployment == Deployment.stg else ""
            copr_repo_name = (
                f"packit/{self.project.namespace}-{self.project.repo}-"
                f"{self.pr_event.pr_id}{stg}"
            )

            payload["artifact"] = {
                "repo-name": self.pr_event.base_repo_name,
                "repo-namespace": self.pr_event.base_repo_namespace,
                "copr-repo-name": copr_repo_name,
                "copr-chroot": chroot,
                "commit-sha": self.pr_event.commit_sha,
                "git-url": self.pr_event.https_url,
                "git-ref": self.pr_event.base_ref,
            }

            logger.debug("Sending testing farm request...")
            logger.debug(payload)

            req = self.send_testing_farm_request(
                TESTING_FARM_TRIGGER_URL, "POST", {}, json.dumps(payload)
            )
            if not req:
                msg = "Failed to post request to testing farm API."
                logger.debug("Failed to post request to testing farm API.")
                r.report(
                    "failure",
                    msg,
                    None,
                    "",
                    check_name=PRCheckName.get_testing_farm_check() + "-" + chroot,
                )
                return HandlerResults(success=False, details={"msg": msg})
            else:
                logger.debug(
                    f"Submitted to testing farm with return code: {req.status_code}"
                )

                """
                Response:
                {
                    "id": "9fa3cbd1-83f2-4326-a118-aad59f5",
                    "success": true,
                    "url": "https://console-testing-farm.apps.ci.centos.org/pipeline/<id>"
                }
                """

                # success set check on pending
                if req.status_code != 200:
                    # something went wrong
                    msg = req.json["message"]
                    r.report(
                        "failure",
                        msg,
                        None,
                        check_name=PRCheckName.get_testing_farm_check() + "-" + chroot,
                    )
                    return HandlerResults(success=False, details={"msg": msg})

                r.report(
                    "pending",
                    "Tests are running ...",
                    None,
                    req.json["url"],
                    check_name=PRCheckName.get_testing_farm_check() + "-" + chroot,
                )

        return HandlerResults(success=True, details={})