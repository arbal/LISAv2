from typing import Dict, List, Optional

from lisa import schema, search_space
from lisa.action import Action, ActionStatus
from lisa.environment import Environment, Environments, load_environments
from lisa.platform_ import WaitMoreResourceError, load_platform
from lisa.testselector import select_testcases
from lisa.testsuite import (
    TestCaseRequirement,
    TestCaseRuntimeData,
    TestResult,
    TestStatus,
    TestSuite,
    TestSuiteMetadata,
)
from lisa.util.logger import get_logger


class LisaRunner(Action):
    def __init__(self, runbook: schema.Runbook) -> None:
        super().__init__()
        self.exit_code: int = 0

        self._runbook = runbook
        self._log = get_logger("runner")

    async def start(self) -> None:  # noqa: C901
        # TODO: Reduce this function's complexity and remove the disabled warning.
        await super().start()
        self.set_status(ActionStatus.RUNNING)

        # select test cases
        selected_test_cases = select_testcases(self._runbook.testcase)

        # create test results
        selected_test_results = self._create_test_results(selected_test_cases)

        # load predefined environments
        candidate_environments = load_environments(self._runbook.environment)

        platform = load_platform(self._runbook.platform)
        # get environment requirements
        self._merge_test_requirements(
            test_results=selected_test_results,
            existing_environments=candidate_environments,
            platform_type=platform.type_name(),
        )

        # there may not need to handle requirements, if all environment are predefined
        prepared_environments = platform.prepare_environments(candidate_environments)

        can_run_results = selected_test_results
        # request environment then run test s
        for environment in prepared_environments:
            try:
                is_needed: bool = False
                can_run_results = [x for x in can_run_results if x.can_run]
                can_run_results.sort(key=lambda x: x.runtime_data.metadata.suite.name)
                new_env_can_run_results = [
                    x for x in can_run_results if x.runtime_data.use_new_environment
                ]

                if not can_run_results:
                    # no left tests, break the loop
                    self._log.debug(
                        f"no more test case to run, skip env [{environment.name}]"
                    )
                    break

                # check if any test need this environment
                if any(
                    case.can_run and case.check_environment(environment, True)
                    for case in can_run_results
                ):
                    is_needed = True

                if not is_needed:
                    self._log.debug(
                        f"env[{environment.name}] skipped "
                        f"as not meet any case requirement"
                    )
                    continue

                try:
                    platform.deploy_environment(environment)
                except WaitMoreResourceError as identifier:
                    self._log.warning(
                        f"[{environment.name}] waiting for more resource: "
                        f"{identifier}, skip assiging case"
                    )
                    continue

                if not environment.is_ready:
                    self._log.warning(
                        f"[{environment.name}] is not deployed successfully, "
                        f"skip assiging case"
                    )
                    continue

                # once environment is ready, check updated capability
                self._log.info(f"start running cases on {environment.name}")
                # try a case need new environment firstly
                for new_env_result in new_env_can_run_results:
                    if new_env_result.check_environment(environment, True):
                        await self._run_suite(
                            environment=environment, cases=[new_env_result]
                        )
                        break

                # grouped test results by test suite.
                grouped_cases: List[TestResult] = []
                current_test_suite: Optional[TestSuiteMetadata] = None
                for test_result in can_run_results:
                    if (
                        test_result.can_run
                        and test_result.check_environment(environment, True)
                        and not test_result.runtime_data.use_new_environment
                    ):
                        if (
                            test_result.runtime_data.metadata.suite
                            != current_test_suite
                            and grouped_cases
                        ):
                            # run last batch cases
                            await self._run_suite(
                                environment=environment, cases=grouped_cases
                            )
                            grouped_cases = []

                        # append new test cases
                        current_test_suite = test_result.runtime_data.metadata.suite
                        grouped_cases.append(test_result)

                if grouped_cases:
                    await self._run_suite(environment=environment, cases=grouped_cases)
            finally:
                if environment and environment.is_ready:
                    platform.delete_environment(environment)

        # not run as there is no fit environment.
        for case in can_run_results:
            if case.can_run:
                reasons = "no available environment"
                if case.check_results and case.check_results.reasons:
                    reasons = f"{reasons}: {case.check_results.reasons}"

                case.set_status(TestStatus.SKIPPED, reasons)

        result_count_dict: Dict[TestStatus, int] = dict()
        for test_result in selected_test_results:
            self._log.info(
                f"{test_result.runtime_data.metadata.full_name:>30}: "
                f"{test_result.status.name:<8} {test_result.message}"
            )
            result_count = result_count_dict.get(test_result.status, 0)
            result_count += 1
            result_count_dict[test_result.status] = result_count

        self._log.info("test result summary")
        self._log.info(f"  TOTAL      : {len(selected_test_results)}")
        for key in TestStatus:
            self._log.info(f"    {key.name:<9}: {result_count_dict.get(key, 0)}")

        self.set_status(ActionStatus.SUCCESS)

        # pass failed count to exit code
        self.exit_code = result_count_dict.get(TestStatus.FAILED, 0)

        # for UT testability
        self._latest_test_results = selected_test_results

    async def stop(self) -> None:
        super().stop()

    async def close(self) -> None:
        super().close()

    async def _run_suite(
        self, environment: Environment, cases: List[TestResult]
    ) -> None:

        assert cases
        suite_metadata = cases[0].runtime_data.metadata.suite
        test_suite: TestSuite = suite_metadata.test_class(
            environment,
            cases,
            suite_metadata,
        )
        for case in cases:
            case.env = environment.name
        await test_suite.start()

    def _create_test_results(
        self, cases: List[TestCaseRuntimeData]
    ) -> List[TestResult]:
        test_results: List[TestResult] = []
        for x in cases:
            test_results.append(TestResult(runtime_data=x))
        return test_results

    def _merge_test_requirements(
        self,
        test_results: List[TestResult],
        existing_environments: Environments,
        platform_type: str,
    ) -> None:
        assert platform_type
        platform_type_set = search_space.SetSpace[str](
            is_allow_set=True, items=[platform_type]
        )
        for test_result in test_results:
            test_req: TestCaseRequirement = test_result.runtime_data.requirement

            # check if there is playform requirement on test case
            if test_req.platform_type and len(test_req.platform_type) > 0:
                check_result = test_req.platform_type.check(platform_type_set)
                if not check_result.result:
                    test_result.set_status(TestStatus.SKIPPED, check_result.reasons)

            if test_result.can_run:
                assert test_req.environment
                # if case need a new env to run, force to create one.
                # if not, get or create one.
                if test_result.runtime_data.use_new_environment:
                    existing_environments.from_requirement(test_req.environment)
                else:
                    existing_environments.get_or_create(test_req.environment)
