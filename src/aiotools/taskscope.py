__all__ = ("TaskScope",)

import asyncio
from asyncio import events, exceptions, tasks
from contextvars import Context
from typing import (
    Any,
    Coroutine,
    Generator,
    Optional,
    Self,
    TypeGuard,
    TypeVar,
)

from .taskcontext import DefaultErrorHandler, ErrorCallback, TaskContext

T = TypeVar("T")


class TaskScope(TaskContext):
    """Asynchronous context manager for managing a scope of subtasks.

    Example use:

        async with asyncio.TaskScope() as scope:
            task1 = scope.create_task(some_coroutine(...))
            task2 = scope.create_task(other_coroutine(...))
        print("Both tasks have completed now.")

    All tasks are awaited when the context manager exits.

    Any exceptions other than `asyncio.CancelledError` raised within
    a task will be handled differently depending on `delegate_errors`.

    If `delegate_errors` is not set, it will run
    `loop.call_exception_handler()`.
    If it is set `None`, it will silently ignore the exception.
    If it is set as a callable function, it will invoke it using the same
    context argument of `loop.call_exception_handler()`.
    """

    _tasks: set[asyncio.Task[Any]]
    _on_completed_fut: Optional[asyncio.Future]
    _base_error: Optional[BaseException]

    def __init__(
        self,
        delegate_errors: Optional[
            ErrorCallback | DefaultErrorHandler
        ] = DefaultErrorHandler.TOKEN,
    ) -> None:
        super().__init__(delegate_errors=delegate_errors)
        # status flags
        self._entered = False
        self._exiting = False
        self._aborting = False
        self._parent_cancel_requested = False
        # taskscope-specifics
        self._base_error = None
        self._on_completed_fut = None
        self._delegate_errors = delegate_errors
        self._has_errors = False

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError(
                f"{type(self).__name__} {self!r} " f"has been already entered"
            )
        self._entered = True

        if self._loop is None:
            self._loop = events.get_running_loop()

        self._parent_task = tasks.current_task(self._loop)
        if self._parent_task is None:
            raise RuntimeError(
                f"{type(self).__name__} {self!r} " f"cannot determine the parent task"
            )

        return self

    async def __aexit__(self, et, exc, tb) -> Optional[bool]:
        assert self._loop is not None
        assert self._parent_task is not None
        self._exiting = True

        if exc is not None and self._is_base_error(exc) and self._base_error is None:
            self._base_error = exc

        propagate_cancellation_error = exc if et is exceptions.CancelledError else None
        if self._parent_cancel_requested:
            # If this flag is set we *must* call uncancel().
            if self._parent_task.uncancel() == 0:
                # If there are no pending cancellations left,
                # don't propagate CancelledError.
                propagate_cancellation_error = None

        if et is not None:
            if not self._aborting:
                # Our parent task is being cancelled:
                #
                #    async with TaskGroup() as g:
                #        g.create_task(...)
                #        await ...  # <- CancelledError
                #
                # or there's an exception in "async with":
                #
                #    async with TaskGroup() as g:
                #        g.create_task(...)
                #        1 / 0
                #
                self.abort()

        # We use while-loop here because "self._on_completed_fut"
        # can be cancelled multiple times if our parent task
        # is being cancelled repeatedly (or even once, when
        # our own cancellation is already in progress)
        while self._tasks:
            if self._on_completed_fut is None:
                self._on_completed_fut = self._loop.create_future()

            try:
                await self._on_completed_fut
            except exceptions.CancelledError as ex:
                if not self._aborting:
                    # Our parent task is being cancelled:
                    #
                    #    async def wrapper():
                    #        async with TaskGroup() as g:
                    #            g.create_task(foo)
                    #
                    # "wrapper" is being cancelled while "foo" is
                    # still running.
                    propagate_cancellation_error = ex
                    self.abort()

            self._on_completed_fut = None

        assert not self._tasks

        if self._base_error is not None:
            raise self._base_error

        # Propagate CancelledError if there is one, except if there
        # are other errors -- those have priority.
        if propagate_cancellation_error and not self._has_errors:
            raise propagate_cancellation_error

        if et is not None and et is not exceptions.CancelledError:
            self._has_errors = True
        return None

    def create_task(
        self,
        coro: Generator[None, None, T] | Coroutine[Any, None, T],
        *,
        name: Optional[str] = None,
        context: Optional[Context] = None,
    ) -> tasks.Task[T]:
        """
        Create a new task in this scope and return it.
        Similar to :func:`asyncio.create_task()`.
        """
        if not self._entered:
            raise RuntimeError(f"{type(self).__name__} {self!r} has not been entered")
        if self._exiting and not self._tasks:
            raise RuntimeError(f"{type(self).__name__} {self!r} is finished")
        return self._create_task(coro, name=name, context=context)

    # Since Python 3.8 Tasks propagate all exceptions correctly,
    # except for KeyboardInterrupt and SystemExit which are
    # still considered special.

    def _is_base_error(self, exc: BaseException) -> TypeGuard[BaseException]:
        assert isinstance(exc, BaseException)
        return isinstance(exc, (SystemExit, KeyboardInterrupt))

    def abort(self) -> None:
        # Trigger cancellation but don't wait.
        self._aborting = True
        for t in self._tasks:
            if not t.done():
                t.cancel()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        assert self._loop is not None
        assert self._parent_task is not None
        self._tasks.discard(task)
        if self._on_completed_fut is not None and not self._tasks:
            if not self._on_completed_fut.done():
                self._on_completed_fut.set_result(True)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return

        self._has_errors = True
        self._handle_task_exception(task)

        is_base_error = self._is_base_error(exc)
        if is_base_error and self._base_error is None:
            self._base_error = exc

        if self._parent_task.done():
            # Not sure if this case is possible, but we want to handle
            # it anyways.
            self._loop.call_exception_handler(
                {
                    "message": f"Task {task!r} has errored out but its parent "
                    f"task {self._parent_task} is already completed",
                    "exception": exc,
                    "task": task,
                }
            )
            return

        if is_base_error:
            # If parent task *is not* being cancelled, it means that we want
            # to manually cancel it to abort whatever is being run right now
            # in the TaskGroup.  But we want to mark parent task as
            # "not cancelled" later in __aexit__.  Example situation that
            # we need to handle:
            #
            #    async def foo():
            #        try:
            #            async with TaskGroup() as g:
            #                g.create_task(crash_soon())
            #                await something  # <- this needs to be canceled
            #                                 #    by the TaskGroup, e.g.
            #                                 #    foo() needs to be cancelled
            #        except Exception:
            #            # Ignore any exceptions raised in the TaskGroup
            #            pass
            #        await something_else     # this line has to be called
            #                                 # after TaskGroup is finished.
            self.abort()
            self._parent_cancel_requested = True
            self._parent_task.cancel()