''' Provides the ``ServerSession`` class.

'''
from __future__ import absolute_import

import logging
log = logging.getLogger(__name__)

from tornado import gen, locks
from bokeh.util.tornado import _DocumentCallbackGroup

import time

def current_time():
    try:
        # python >=3.3 only
        return time.monotonic()
    except:
        # if your python is old, don't set your clock backward!
        return time.time()

def _needs_document_lock(func):
    '''Decorator that adds the necessary locking and post-processing
       to manipulate the session's document. Expects to decorate a
       non-coroutine method on ServerSession and transforms it
       into a coroutine.
    '''
    @gen.coroutine
    def _needs_document_lock_wrapper(self, *args, **kwargs):
        with (yield self._lock.acquire()):
            if self._pending_writes is not None:
                raise RuntimeError("internal class invariant violated: _pending_writes " + \
                                   "should be None if lock is not held")
            self._pending_writes = []
            try:
                result = func(self, *args, **kwargs)
                while True:
                    try:
                        future = gen.convert_yielded(result)
                    except gen.BadYieldError:
                        # result is not a yieldable thing, we are done
                        break
                    else:
                        result = yield future
            finally:
                # we want to be very sure we reset this or we'll
                # keep hitting the RuntimeError above as soon as
                # any callback goes wrong
                pending_writes = self._pending_writes
                self._pending_writes = None
            for p in pending_writes:
                yield p
        raise gen.Return(result)
    return _needs_document_lock_wrapper

class ServerSession(object):
    ''' Hosts an application "instance" (an instantiated Document) for one or more connections.

    '''

    def __init__(self, session_id, document, io_loop=None):
        if session_id is None:
            raise ValueError("Sessions must have an id")
        if document is None:
            raise ValueError("Sessions must have a document")
        self._id = session_id
        self._document = document
        self._loop = io_loop
        self._subscribed_connections = set()
        self._last_unsubscribe_time = current_time()
        self._lock = locks.Lock()
        self._current_patch = None
        self._current_patch_connection = None
        self._document.on_change_dispatch_to(self)
        self._callbacks = _DocumentCallbackGroup(io_loop)
        self._pending_writes = None
        self._destroyed = False

        wrapped_callbacks = self._wrap_session_callbacks(self._document.session_callbacks)
        self._callbacks.add_session_callbacks(wrapped_callbacks)

    @property
    def document(self):
        return self._document

    @property
    def id(self):
        return self._id

    @property
    def destroyed(self):
        return self._destroyed

    def destroy(self):
        self._destroyed = True
        self._document.remove_on_change(self)
        self._callbacks.remove_all_callbacks()

    def subscribe(self, connection):
        """This should only be called by ServerConnection.subscribe_session or our book-keeping will be broken"""
        self._subscribed_connections.add(connection)

    def unsubscribe(self, connection):
        """This should only be called by ServerConnection.unsubscribe_session or our book-keeping will be broken"""
        self._subscribed_connections.discard(connection)
        self._last_unsubscribe_time = current_time()

    @property
    def connection_count(self):
        return len(self._subscribed_connections)

    @property
    def seconds_since_last_unsubscribe(self):
        return current_time() - self._last_unsubscribe_time

    @_needs_document_lock
    def with_document_locked(self, func, *args, **kwargs):
        ''' Asynchronously locks the document and runs the function with it locked.'''
        return func(*args, **kwargs)

    def _wrap_document_callback(self, callback):
        def wrapped_callback(*args, **kwargs):
            return self.with_document_locked(callback, *args, **kwargs)
        return wrapped_callback

    def _wrap_session_callback(self, callback):
        wrapped = self._wrap_document_callback(callback.callback)
        return callback._copy_with_changed_callback(wrapped)

    def _wrap_session_callbacks(self, callbacks):
        wrapped = []
        for cb in callbacks:
            wrapped.append(self._wrap_session_callback(cb))
        return wrapped

    def _document_patched(self, event):
        may_suppress = self._current_patch is not None and \
                       self._current_patch.should_suppress_on_change(event)

        if self._pending_writes is None:
            raise RuntimeError("_pending_writes should be non-None when we have a document lock, and we should have the lock when the document changes")

        # TODO (havocp): our "change sync" protocol is flawed
        # because if both sides change the same attribute at the
        # same time, they will each end up with the state of the
        # other and their final states will differ.
        for connection in self._subscribed_connections:
            if may_suppress and connection is self._current_patch_connection:
                pass #log.debug("Not sending notification back to client %r for a change it requested", connection)
            else:
                self._pending_writes.append(connection.send_patch_document(event))

    @_needs_document_lock
    def _handle_pull(self, message, connection):
        log.debug("Sending pull-doc-reply from session %r", self.id)
        return connection.protocol.create('PULL-DOC-REPLY', message.header['msgid'], self.document)

    def _session_callback_added(self, event):
        wrapped = self._wrap_session_callback(event.callback)
        self._callbacks.add_session_callback(wrapped)

    def _session_callback_removed(self, event):
        self._callbacks.remove_session_callback(event.callback)

    @classmethod
    def pull(cls, message, connection):
        ''' Handle a PULL-DOC, return a Future with work to be scheduled. '''
        return connection.session._handle_pull(message, connection)

    @_needs_document_lock
    def _handle_push(self, message, connection):
        log.debug("pushing doc to session %r", self.id)
        message.push_to_document(self.document)
        return connection.ok(message)

    @classmethod
    def push(cls, message, connection):
        ''' Handle a PUSH-DOC, return a Future with work to be scheduled. '''
        return connection.session._handle_push(message, connection)

    @_needs_document_lock
    def _handle_patch(self, message, connection):
        self._current_patch = message
        self._current_patch_connection = connection
        try:
            message.apply_to_document(self.document)
        finally:
            self._current_patch = None
            self._current_patch_connection = None

        return connection.ok(message)

    @classmethod
    def patch(cls, message, connection):
        ''' Handle a PATCH-DOC, return a Future with work to be scheduled. '''
        return connection.session._handle_patch(message, connection)
