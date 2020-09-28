from __future__ import annotations

import unittest
from abc import ABCMeta
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Type, Union

from retry.api import retry_call  # type: ignore

from lisa import notifier, schema, search_space
from lisa.action import Action, ActionStatus
from lisa.environment import EnvironmentSpace
from lisa.feature import Feature
from lisa.operating_system import OperatingSystem
from lisa.util import LisaException, constants, set_filtered_fields
from lisa.util.logger import get_logger
from lisa.util.perf_timer import create_timer

if TYPE_CHECKING:
    from lisa.environment import Environment


TestStatus = Enum(
    "TestStatus", ["NOTRUN", "RUNNING", "FAILED", "PASSED", "SKIPPED", "ATTEMPTED"]
)

_all_suites: Dict[str, TestSuiteMetadata] = dict()
_all_cases: Dict[str, TestCaseMetadata] = dict()


class SkipTestCaseException(LisaException):
    pass


@dataclass
class TestResultMessage(notifier.MessageBase):
    type: str = "TestResult"
    status: TestStatus = TestStatus.NOTRUN
    message: str = ""
    env: str = ""


@dataclass
class TestResult:
    runtime_data: TestCaseRuntimeData
    status: TestStatus = TestStatus.NOTRUN
    elapsed: float = 0
    message: str = ""
    env: str = ""
    check_results: Optional[search_space.ResultReason] = None

    @property
    def can_run(self) -> bool:
        return self.status == TestStatus.NOTRUN

    def set_status(
        self, new_status: TestStatus, message: Union[str, List[str]]
    ) -> None:
        if message:
            if isinstance(message, str):
                message = [message]
            if self.message:
                message.insert(0, self.message)
            self.message = "\n".join(message)
        if self.status != new_status:
            self.status = new_status

            fields = ["status", "elapsed", "message", "env"]
            result_message = TestResultMessage()
            set_filtered_fields(self, result_message, fields=fields)
            notifier.notify(result_message)

    def check_environment(
        self, environment: Environment, save_reason: bool = False
    ) -> bool:
        requirement = self.runtime_data.metadata.requirement
        assert requirement.environment
        check_result = requirement.environment.check(environment.capability)
        if check_result.result and requirement.os_type and environment.is_ready:
            for node in environment.nodes.list():
                # use __mro__ to match any super types.
                # for example, Ubuntu satisifies Linux
                node_os_capability = search_space.SetSpace[Type[OperatingSystem]](
                    is_allow_set=True, items=type(node.os).__mro__
                )
                check_result.merge(
                    requirement.os_type.check(node_os_capability), "os_type"
                )
                if not check_result.result:
                    break
        if save_reason:
            if self.check_results:
                self.check_results.merge(check_result)
            else:
                self.check_results = check_result
        return check_result.result


@dataclass
class TestCaseRequirement:
    environment: Optional[EnvironmentSpace] = None
    platform_type: Optional[search_space.SetSpace[str]] = None
    os_type: Optional[search_space.SetSpace[Type[OperatingSystem]]] = None


def simple_requirement(
    min_count: int = 1,
    min_nic_count: int = 1,
    node: Optional[schema.NodeSpace] = None,
    supported_platform_type: Optional[List[str]] = None,
    unsupported_platform_type: Optional[List[str]] = None,
    supported_os: Optional[List[Type[OperatingSystem]]] = None,
    unsupported_os: Optional[List[Type[OperatingSystem]]] = None,
    supported_features: Optional[List[Type[Feature]]] = None,
    unsupported_features: Optional[List[Type[Feature]]] = None,
) -> TestCaseRequirement:
    """
    define a simple requirement to support most test cases.
    """
    if node is None:
        node = schema.NodeSpace()

    node.node_count = search_space.IntRange(min=min_count)
    node.nic_count = search_space.IntRange(min=min_nic_count)
    if supported_features:
        node.features = search_space.SetSpace[str](
            is_allow_set=True,
            items=[x.name() for x in supported_features],
        )
    if unsupported_features:
        node.excluded_features = search_space.SetSpace[str](
            is_allow_set=False,
            items=[x.name() for x in unsupported_features],
        )
    nodes: List[schema.NodeSpace] = [node]

    platform_types = search_space.create_set_space(
        supported_platform_type, unsupported_platform_type, "platform type"
    )

    os = search_space.create_set_space(supported_os, unsupported_os, "operating system")

    return TestCaseRequirement(
        environment=EnvironmentSpace(nodes=nodes),
        platform_type=platform_types,
        os_type=os,
    )


DEFAULT_REQUIREMENT = simple_requirement()


class TestSuiteMetadata:
    def __init__(
        self,
        area: str,
        category: str,
        description: str,
        tags: List[str],
        name: str = "",
        requirement: TestCaseRequirement = DEFAULT_REQUIREMENT,
    ) -> None:
        self.name = name
        self.cases: List[TestCaseMetadata] = []

        self.area = area
        self.category = category
        if tags:
            self.tags = tags
        else:
            self.tags = []
        self.description = description
        self.requirement = requirement

    def __call__(self, test_class: Type[TestSuite]) -> Callable[..., object]:
        self.test_class = test_class
        if not self.name:
            self.name = test_class.__name__
        _add_suite_metadata(self)

        @wraps(self.test_class)
        def wrapper(
            test_class: Type[TestSuite],
            environment: Environment,
            cases: List[TestResult],
            metadata: TestSuiteMetadata,
        ) -> TestSuite:
            return test_class(environment, cases, metadata)

        return wrapper


class TestCaseMetadata:
    def __init__(
        self,
        description: str,
        priority: int = 2,
        requirement: Optional[TestCaseRequirement] = None,
    ) -> None:
        self.priority = priority
        self.description = description
        if requirement:
            self.requirement = requirement

    def __getattr__(self, key: str) -> Any:
        # inherit all attributes of test suite
        assert self.suite, "suite is not set before use metadata"
        return getattr(self.suite, key)

    def __call__(self, func: Callable[..., None]) -> Callable[..., None]:
        self.name = func.__name__
        self.full_name = func.__qualname__

        self._func = func
        _add_case_metadata(self)

        @wraps(self._func)
        def wrapper(*args: object) -> None:
            func(*args)

        return wrapper

    def set_suite(self, suite: TestSuiteMetadata) -> None:
        self.suite: TestSuiteMetadata = suite


class TestCaseRuntimeData:
    def __init__(self, metadata: TestCaseMetadata):
        self.metadata = metadata

        # all runtime setting fields
        self.select_action: str = ""
        self.times: int = 1
        self.retry: int = 0
        self.use_new_environment: bool = False
        self.ignore_failure: bool = False
        self.environment_name: str = ""

    def __getattr__(self, key: str) -> Any:
        # inherit all attributes of metadata
        assert self.metadata
        return getattr(self.metadata, key)

    def clone(self) -> TestCaseRuntimeData:
        cloned = TestCaseRuntimeData(self.metadata)
        fields = [
            constants.TESTCASE_SELECT_ACTION,
            constants.TESTCASE_TIMES,
            constants.TESTCASE_RETRY,
            constants.TESTCASE_USE_NEW_ENVIRONMENT,
            constants.TESTCASE_IGNORE_FAILURE,
            constants.ENVIRONMENT,
        ]
        set_filtered_fields(self, cloned, fields)
        return cloned


class TestSuite(unittest.TestCase, Action, metaclass=ABCMeta):
    def __init__(
        self,
        environment: Environment,
        case_results: List[TestResult],
        metadata: TestSuiteMetadata,
    ) -> None:
        super().__init__()
        self.environment = environment
        # test cases to run, must be a test method in this class.
        self.case_results = case_results
        self._metadata = metadata
        self._should_stop = False
        self.log = get_logger("suite", metadata.name)

    def before_suite(self) -> None:
        pass

    def after_suite(self) -> None:
        pass

    def before_case(self) -> None:
        pass

    def after_case(self) -> None:
        pass

    async def start(self) -> None:  # noqa: C901
        # TODO: Reduce this function's complexity and remove the disabled warning.
        suite_error_message = ""
        is_suite_continue = True

        timer = create_timer()
        try:
            self.before_suite()
        except Exception as identifier:
            suite_error_message = f"before_suite: {identifier}"
            is_suite_continue = False
        self.log.debug(f"before_suite end with {timer}")

        #  replace to case's logger temporarily
        suite_log = self.log
        for case_result in self.case_results:
            case_name = case_result.runtime_data.name
            test_method = getattr(self, case_name)
            self.log = get_logger("case", f"{case_result.runtime_data.full_name}")

            self.log.info("started")
            is_continue: bool = is_suite_continue
            total_timer = create_timer()

            if is_continue:
                timer = create_timer()
                try:
                    retry_call(
                        self.before_case,
                        exceptions=Exception,
                        tries=case_result.runtime_data.retry + 1,
                        logger=self.log,
                    )
                except Exception as identifier:
                    self.log.error("before_case: ", exc_info=identifier)
                    case_result.set_status(
                        TestStatus.SKIPPED, f"before_case: {identifier}"
                    )
                    is_continue = False
                case_result.elapsed = timer.elapsed()
                self.log.debug(f"before_case end with {timer}")
            else:
                case_result.set_status(TestStatus.SKIPPED, suite_error_message)

            if is_continue:
                timer = create_timer()
                try:
                    retry_call(
                        test_method,
                        exceptions=Exception,
                        tries=case_result.runtime_data.retry + 1,
                        logger=self.log,
                    )
                    case_result.set_status(TestStatus.PASSED, "")
                except Exception as identifier:
                    if case_result.runtime_data.ignore_failure:
                        self.log.info(f"case failed and ignored: {identifier}")
                        case_result.set_status(TestStatus.ATTEMPTED, f"{identifier}")
                    else:
                        self.log.error("case failed", exc_info=identifier)
                        case_result.set_status(
                            TestStatus.FAILED, f"failed: {identifier}"
                        )
                case_result.elapsed = timer.elapsed()
                self.log.debug(f"case end with {timer}")

            timer = create_timer()
            try:
                retry_call(
                    self.after_case,
                    exceptions=Exception,
                    tries=case_result.runtime_data.retry + 1,
                    logger=self.log,
                )
            except Exception as identifier:
                # after case doesn't impact test case result.
                self.log.error("after_case failed", exc_info=identifier)
            self.log.debug(f"after_case end with {timer}")

            case_result.elapsed = total_timer.elapsed()
            self.log.info(
                f"result: {case_result.status.name}, " f"elapsed: {total_timer}"
            )

            if self._should_stop:
                self.log.info("received stop message, stop run")
                self.set_status(ActionStatus.STOPPED)
                break

        self.log = suite_log
        timer = create_timer()
        try:
            self.after_suite()
        except Exception as identifier:
            # after_suite doesn't impact test case result, and can continue
            self.log.error("after_suite failed", exc_info=identifier)
        self.log.debug(f"after_suite end with {timer}")

    async def stop(self) -> None:
        self.set_status(ActionStatus.STOPPING)
        self._should_stop = True

    async def close(self) -> None:
        pass


def get_suites_metadata() -> Dict[str, TestSuiteMetadata]:
    return _all_suites


def get_cases_metadata() -> Dict[str, TestCaseMetadata]:
    return _all_cases


def _add_suite_metadata(metadata: TestSuiteMetadata) -> None:
    if metadata.name:
        key = metadata.name
    else:
        key = metadata.test_class.__name__
    exist_metadata = _all_suites.get(key)
    if exist_metadata is None:
        _all_suites[key] = metadata
    else:
        raise LisaException(
            f"duplicate test class name: {key}, "
            f"new: [{metadata}], exists: [{exist_metadata}]"
        )

    class_prefix = f"{key}."
    for test_case in _all_cases.values():
        if test_case.full_name.startswith(class_prefix):
            _add_case_to_suite(metadata, test_case)
    log = get_logger("init", "test")
    log.info(
        f"registered test suite '{key}' "
        f"with test cases: '{', '.join([case.name for case in metadata.cases])}'"
    )


def _add_case_metadata(metadata: TestCaseMetadata) -> None:

    full_name = metadata.full_name
    if _all_cases.get(full_name) is None:
        _all_cases[full_name] = metadata
    else:
        raise LisaException(f"duplicate test class name: {full_name}")

    # this should be None in current observation.
    # the methods are loadded prior to test class
    # in case logic is changed, so keep this logic
    #   to make two collection consistent.
    class_name = full_name.split(".")[0]
    test_suite = _all_suites.get(class_name)
    if test_suite:
        log = get_logger("init", "test")
        log.debug(f"add case '{metadata.name}' to suite '{test_suite.name}'")
        _add_case_to_suite(test_suite, metadata)


def _add_case_to_suite(
    test_suite: TestSuiteMetadata, test_case: TestCaseMetadata
) -> None:
    test_case.suite = test_suite
    test_suite.cases.append(test_case)
