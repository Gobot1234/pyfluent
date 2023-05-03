import logging
from time import sleep
from typing import Any, Iterator, Tuple
import warnings

from ansys.fluent.core.services.datamodel_se import PyCallableStateObject

datamodel_logger = logging.getLogger("ansys.fluent.services.datamodel")


def _new_command_for_task(task, session):
    class NewCommandError(Exception):
        def __init__(self, task_name):
            super().__init__(f"Could not create command for meshing task {task_name}")

    task_cmd_name = task.CommandName()
    cmd_creator = getattr(session, task_cmd_name)
    if cmd_creator:
        new_cmd = cmd_creator.create_instance()
        if new_cmd:
            return new_cmd
    raise NewCommandError(task._name_())


def init_task_accessors(obj):
    print("init_task_accessors")
    for task in obj.ordered_children(recompute=True):
        py_name = task.python_name()
        print("py_name:", py_name)
        obj._python_task_names.append(py_name)
        if not getattr(obj, py_name, None):
            print("adding", py_name, type(task))
            setattr(obj, py_name, task)
        else:
            print("Could not add task", py_name, type(getattr(obj, py_name, None)))
        init_task_accessors(task)


def refresh_task_accessors(obj):
    old_task_names = set(obj._python_task_names)
    print("refresh_task_accessors old_task_names:", old_task_names)
    tasks = obj.ordered_children(recompute=True)
    current_task_names = [task.python_name() for task in tasks]
    print("current_task_names:", current_task_names)
    current_task_name_set = set(current_task_names)
    created_task_names = current_task_name_set - old_task_names
    deleted_task_names = old_task_names - current_task_name_set
    for task_name in deleted_task_names:
        try:
            delattr(obj, task_name)
        except AttributeError:
            pass
    for task_name in created_task_names:
        if not getattr(obj, task_name, None):
            print("Add task", task_name)
            setattr(obj, task_name, tasks[current_task_names.index(task_name)])
        else:
            print("Could not add task", task_name, type(getattr(obj, task_name, None)))
    obj._python_task_names = current_task_names
    print("updated_task_names:", obj._python_task_names)
    for task in tasks:
        print("next task", task.python_name(), id(task))
        refresh_task_accessors(task)


class BaseTask:
    """Base class Task representation for wrapping a Workflow TaskObject instance,
    adding methods to discover more about the relationships between TaskObjects.
    Methods
    -------
    get_direct_upstream_tasks()
    get_direct_downstream_tasks()
    ordered_children()
    inactive_ordered_children()
    get_id()
    get_idx()
    __getattr__(attr)
    __setattr__(attr, value)
    __dir__()
    __call__()
    """

    def __init__(self, command_source, task) -> None:
        self.__dict__.update(
            dict(
                _command_source=command_source,
                _workflow=command_source._workflow,
                _source=command_source._command_source,
                _task=task,
                _cmd=None,
                _python_name=None,
                _python_task_names=[],
                _ordered_children=[],
                _task_list=[],
            )
        )

    def get_direct_upstream_tasks(self) -> list:
        """Get the list of tasks upstream of this one and directly connected by
        a data dependency.

        Returns
        -------
        upstreams : list
            Upstream task list.
        """
        return self._tasks_with_matching_attributes(
            attr="requiredInputs", other_attr="outputs"
        )

    def get_direct_upstream_tasks(self) -> list:
        """Get the list of tasks upstream of this one and directly connected by a data dependency.

        Returns
        -------
        upstreams : list
            Upstream task list.
        """
        return self._tasks_with_matching_attributes(
            attr="requiredInputs", other_attr="outputs"
        )

    def get_direct_downstream_tasks(self) -> list:
        """Get the list of tasks downstream of this one and directly connected
        by a data dependency.

        Returns
        -------
        downstreams : list
            Downstream task list.
        """
        return self._tasks_with_matching_attributes(
            attr="outputs", other_attr="requiredInputs"
        )

    def ordered_children(self, recompute=False) -> list:
        """Get the ordered task list held by this task. Sorting is in terms
        of the workflow order and only includes this task's top-level tasks, while other tasks
        can be obtained by calling ordered_children() on a parent task. Given the
        workflow::

            Workflow
            ├── A
            ├── B
            │   ├── C
            │   └── D
            └── E

        C and D are the ordered children of task B.

        Returns
        -------
        children : list
            Ordered children.
        """
        if recompute:

            def task_by_id(mappings):
                def _task_by_id(task_id):
                    if task_id in mappings:
                        return mappings[task_id]
                    try:
                        return self._command_source._task_by_id(task_id)
                    except BaseException:
                        pass

                return _task_by_id

            task_list = self._task.TaskList()
            if task_list != self._task_list:
                mappings = {
                    k: v for k, v in zip(self._task_list, self._ordered_children)
                }
                self._ordered_children = list(
                    filter(None, map(task_by_id(mappings), task_list))
                )
                self._task_list = task_list
        return self._ordered_children

    def inactive_ordered_children(self) -> list:
        return []

    def child_task_python_names(self):
        return self._python_task_names

    def get_id(self) -> str:
        """Get the unique string identifier of this task, as it is in the
        meshing application.

        Returns
        -------
        identifier : str
            The string identifier.
        """
        workflow_state = self._command_source._workflow_state()
        for k, v in workflow_state.items():
            if isinstance(v, dict) and "_name_" in v:
                if v["_name_"] == self.name():
                    type_, id_ = k.split(":")
                    if type_ == "TaskObject":
                        return id_

    def get_idx(self) -> int:
        """Get the unique integer index of this task, as it is in the meshing
        application.

        Returns
        -------
        index : int
            The integer index.
        """
        return int(self.get_id()[len("TaskObject") :])

    def python_name(self):
        if not self._python_name:
            try:
                this_command = self._command()
                # temp reuse helpString
                self._python_name = this_command.get_attr("helpString")
            except BaseException:
                pass
        return self._python_name

    def __getattr__(self, attr):
        try:
            result = getattr(self._task, attr)
            if result:
                return result
        except AttributeError:
            pass
        try:
            return ArgumentWrapper(self, attr)
        except BaseException as ex:
            print(str(ex))

    def __setattr__(self, attr, value):
        datamodel_logger.debug(f"BaseTask.__setattr__({attr}, {value})")
        if attr in self.__dict__:
            self.__dict__[attr] = value
        else:
            setattr(self._task, attr, value)

    def __dir__(self):
        return sorted(
            set(list(self.__dict__.keys()) + dir(type(self)) + dir(self._task))
        )

    def __call__(self, **kwds) -> Any:
        if kwds:
            self._task.Arguments.set_state(**kwds)
        return self._task.Execute()

    def _tasks_with_matching_attributes(self, attr: str, other_attr: str) -> list:
        this_command = self._command()
        attrs = this_command.get_attr(attr)
        if not attrs:
            return []
        attrs = set(attrs)
        tasks = [
            task
            for task in self._command_source.ordered_children()
            if task.name() != self.name()
        ]
        matches = []
        for task in tasks:
            command = task._command()
            other_attrs = command.get_attr(other_attr)
            if other_attrs and (attrs & set(other_attrs)):
                matches.append(task)
        return matches


class TaskContainer(PyCallableStateObject):
    """Wrap a workflow TaskObject container.

    Methods
    -------
    __iter__()
    __getitem__(attr)
    __getattr__(attr)
    __dir__()
    """

    def __init__(self, command_source):
        self._container = command_source
        self._task_container = command_source._workflow.TaskObject

    def __iter__(self) -> Iterator[BaseTask]:
        """Yield the next child object.
        Yields
        ------
        Iterator[BaseTask]
            Iterator of child objects.
        """
        for name in self._get_child_object_display_names():
            yield self[name]

    def __getitem__(self, name):
        datamodel_logger.debug(f"TaskContainer.__getitem__({name})")
        return makeTask(self._container, name)

    def __getattr__(self, attr):
        return getattr(self._task_container, attr)

    def __dir__(self):
        return sorted(
            set(
                list(self.__dict__.keys()) + dir(type(self)) + dir(self._task_container)
            )
        )


class ArgumentsWrapper(PyCallableStateObject):
    def __init__(self, task):
        self._task = task

    def set_state(self, args):
        self._task.Arguments.set_state(args)

    def update_dict(self, args):
        self._task.Arguments.update_dict(args)

    def get_state(self, explicit_only=False):
        return (
            self._task.Arguments() if explicit_only else self._task._command_arguments()
        )

    def __getattr__(self, attr):
        return getattr(self._task._command_arguments, attr)

    def __setitem__(self, key, value):
        self._task._command_arguments.__setitem__(key, value)


class ArgumentWrapper(PyCallableStateObject):
    def __init__(self, task, arg):
        self._task = task
        self._arg_name = arg
        self._arg = getattr(task._command_arguments, arg)
        if self._arg is None:
            raise RuntimeError(f"{arg} is not an argument")

    def set_state(self, value):
        self._task.Arguments.update_dict({self._arg_name: value})

    def get_state(self, explicit_only=False):
        return self._task.Arguments()[self._arg_name] if explicit_only else self._arg()

    def __getattr__(self, attr):
        return getattr(self._arg, attr)


class CommandTask(BaseTask):
    """Intermediate base class task representation for wrapping a Workflow TaskObject instance,
    adding attributes related to commanding. Classes without these attributes cannot be commanded.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    @property
    def CommandArguments(self):
        warnings.warn("CommandArguments", DeprecationWarning)
        return self._refreshed_command()

    @property
    def _command_arguments(self):
        return self._refreshed_command()

    @property
    def arguments(self):
        return ArgumentsWrapper(self)

    def _refreshed_command(self):
        task_arg_state = self._task.Arguments.get_state()
        cmd = self._command()
        if task_arg_state:
            cmd.set_state(task_arg_state)
        return _MakeReadOnly(self._cmd_sub_items_read_only(cmd))

    def _cmd_sub_items_read_only(self, cmd):
        for item in cmd():
            if type(getattr(cmd, item).get_state()) == dict:
                setattr(cmd, item, self._cmd_sub_items_read_only(getattr(cmd, item)))
            setattr(cmd, item, _MakeReadOnly(getattr(cmd, item)))
        return cmd

    def _command(self):
        if not self._cmd:
            self._cmd = _new_command_for_task(self._task, self._source)
        return self._cmd


class SimpleTask(CommandTask):
    """Simple task representation for wrapping a Workflow TaskObject
    instance of TaskType Simple.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    def ordered_children(self, recompute=False) -> list:
        """Get the ordered task list held by the workflow. SimpleTasks have no TaskList"""
        return []


class CompoundChild(SimpleTask):
    """Compound Child representation for wrapping a Workflow TaskObject
    instance of TaskType Compound Child.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    def python_name(self):
        pass


class CompositeTask(BaseTask):
    """Composite task representation for wrapping a Workflow TaskObject
    instance of TaskType Composite.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    @property
    def CommandArguments(self):
        warnings.warn("CommandArguments", DeprecationWarning)
        return {}

    @property
    def _command_arguments(self):
        return {}

    @property
    def arguments(self):
        return {}


class ConditionalTask(CommandTask):
    """Conditional task representation for wrapping a Workflow TaskObject
    instance of TaskType Conditional.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    def inactive_ordered_children(self) -> list:
        """Get the inactive ordered task list held by this task.

        Returns
        -------
        children : list
            Inactive ordered children.
        """
        return [
            self._command_source._task_by_id(task_id)
            for task_id in self._task.InactiveTaskList()
        ]


class CompoundTask(CommandTask):
    """Compound task representation for wrapping a Workflow TaskObject
    instance of TaskType Compound.
    """

    def __init__(self, command_source, task) -> None:
        super().__init__(command_source, task)

    def add_child(self, state=None):
        state = state or {}
        state.update({"AddChild": "yes"})
        self._task.Arguments.set_state(state)

    def add_child_and_update(self, state=None):
        self.add_child(state)
        self._task.AddChildAndUpdate()
        return self.last_child()

    def last_child(self):
        children = self.ordered_children()
        if children:
            return children[-1]

    def compound_child(self, name):
        try:
            return next(filter(lambda t: t.name() == name, self.ordered_children()))
        except StopIteration:
            pass


def makeTask(command_source, name: str) -> BaseTask:
    task = command_source._workflow.TaskObject[name]
    task_type = task.TaskType()
    kinds = {
        "Simple": SimpleTask,
        "Compound Child": CompoundChild,
        "Compound": CompoundTask,
        "Composite": CompositeTask,
        "Conditional": ConditionalTask,
    }
    kind = kinds[task.TaskType()]
    if not kind:
        message = (
            "Unhandled empty workflow task type."
            if not task.TaskType()
            else f"Unhandled workflow task type, {task.TaskType()}."
        )
        raise RuntimeError(message)
    return kind(command_source, task)


class WorkflowWrapper:
    """Wrap a Workflow object, adding methods to discover more about the
    relationships between TaskObjects.

    Methods
    -------
    task(name)
    ordered_children()
    __getattr__(attr)
    __dir__()
    __call__()
    """

    def __init__(self, workflow, command_source):
        self._workflow = workflow
        self._command_source = command_source
        self._python_task_names = []
        self.updating = False
        self._ordered_children = []
        self._task_list = []

    def task(self, name: str) -> BaseTask:
        """Get a TaskObject by name, in a BaseTask wrapper. The wrapper adds extra
        functionality.

        Parameters
        ----------
        name : str
            Task name - the display name, not the internal ID.

        Returns
        -------
        task : BaseTask
            wrapped task object.
        """
        return makeTask(self, name)

    @property
    def TaskObject(self) -> TaskContainer:
        # missing from dir
        """Get a TaskObject container wrapper that 'holds' the underlying
        TaskObjects.

        The wrapper adds extra functionality.
        """
        return TaskContainer(self)

    def ordered_children(self, recompute=False) -> list:
        """Get the ordered task list held by the workflow. Sorting is in terms
        of the workflow order and only includes the top-level tasks, while other tasks
        can be obtained by calling ordered_children() on a parent task. Given the
        workflow:

            Workflow
            ├── A
            ├── B
            │   ├── C
            │   └── D
            └── E

        the ordered children of the workflow are A, B, E, while B has ordered children
        C and D.
        """
        if recompute:
            workflow_state, task_list = self._workflow_and_task_list_state()

            def task_by_id(mappings):
                def _task_by_id(task_id):
                    if task_id in mappings:
                        return mappings[task_id]
                    try:
                        return self._task_by_id_impl(task_id, workflow_state)
                    except BaseException:
                        pass

                return _task_by_id

            if task_list != self._task_list:
                mappings = {
                    k: v for k, v in zip(self._task_list, self._ordered_children)
                }
                self._ordered_children = list(
                    filter(None, map(task_by_id(mappings), task_list))
                )
                self._task_list = task_list
        return self._ordered_children

    def child_task_python_names(self):
        return self._python_task_names

    def inactive_ordered_children(self) -> list:
        """Get the inactive ordered task list held by this task.

        Returns
        -------
        children : list
            Inactive ordered children.
        """
        return []

    def __getattr__(self, attr):
        """Delegate attribute lookup to the wrapped workflow object
        Parameters
        ----------
        attr : str
            An attribute not defined in WorkflowWrapper
        """
        return self._attr_from_wrapped_workflow(
            attr
        )  # or self._task_with_cmd_matching_help_string(attr)

    def __dir__(self):
        """Override the behaviour of dir to include attributes in
        WorkflowWrapper and the underlying workflow."""
        return sorted(
            set(list(self.__dict__.keys()) + dir(type(self)) + dir(self._workflow))
        )

    def __call__(self):
        """Delegate calls to the underlying workflow."""
        return self._workflow()

    def _workflow_state(self):
        return self._workflow()

    def _workflow_and_task_list_state(self) -> Tuple[dict, dict]:
        workflow_state = self._workflow_state()
        workflow_state_workflow = workflow_state["Workflow"]
        return (workflow_state, workflow_state_workflow["TaskList"])

    def _task_by_id_impl(self, task_id, workflow_state):
        task_key = "TaskObject:" + task_id
        task_state = workflow_state[task_key]
        return self.task(task_state["_name_"])

    def _task_by_id(self, task_id):
        workflow_state = self._workflow_state()
        return self._task_by_id_impl(task_id, workflow_state)

    def _attr_from_wrapped_workflow(self, attr):
        try:
            result = getattr(self._workflow, attr)
            if result:
                return result
        except AttributeError:
            pass

    # def _task_with_cmd_matching_help_string(self, help_string):
    #    init_task_accessors(self)
    #    return getattr(self, help_string)

    def _new_workflow(self, name: str, dynamic_interface: bool):
        self._workflow.InitializeWorkflow(WorkflowType=name)

    def _initialize_methods(self, dynamic_interface: bool):
        init_task_accessors(self)
        if dynamic_interface:

            def refresh_after_sleep(_):
                while self.updating:
                    print("Already refreshing, ...")
                    sleep(0.1)
                self.updating = True
                print("Call refresh_task_accessors")
                refresh_task_accessors(self)
                self.updating = False

            self.add_on_affected(refresh_after_sleep)


class _MakeReadOnly:
    """Removes 'set_state()' attribute to implement read-only behaviour."""

    _unwanted_attr = ["set_state", "setState"]

    def __init__(self, cmd):
        self._cmd = cmd

    def is_read_only(self):
        return True

    def __getattr__(self, attr):
        if attr in _MakeReadOnly._unwanted_attr:
            raise AttributeError("Command Arguments are read-only.")
        return getattr(self._cmd, attr)

    def __dir__(self):
        returned_list = sorted(
            set(list(self.__dict__.keys()) + dir(type(self)) + dir(self._cmd))
        )
        for attr in _MakeReadOnly._unwanted_attr:
            if attr in returned_list:
                returned_list.remove(attr)
        return returned_list

    def __call__(self):
        return self._cmd()
