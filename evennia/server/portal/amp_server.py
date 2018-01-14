"""
The Evennia Portal service acts as an AMP-server, handling AMP
communication to the AMP clients connecting to it (by default
these are the Evennia Server and the evennia launcher).

"""
import os
import sys
from twisted.internet import protocol
from evennia.server.portal import amp
from subprocess import Popen, STDOUT, PIPE
from evennia.utils import logger


def getenv():
    """
    Get current environment and add PYTHONPATH.

    Returns:
        env (dict): Environment global dict.

    """
    sep = ";" if os.name == 'nt' else ":"
    env = os.environ.copy()
    env['PYTHONPATH'] = sep.join(sys.path)
    return env


class AMPServerFactory(protocol.ServerFactory):

    """
    This factory creates AMP Server connection. This acts as the 'Portal'-side communication to the
    'Server' process.

    """
    noisy = False

    def __init__(self, portal):
        """
        Initialize the factory. This is called as the Portal service starts.

        Args:
            portal (Portal): The Evennia Portal service instance.
            protocol (Protocol): The protocol the factory creates
                instances of.

        """
        self.portal = portal
        self.protocol = AMPServerProtocol
        self.broadcasts = []
        self.server_connection = None
        self.disconnect_callbacks = {}

    def buildProtocol(self, addr):
        """
        Start a new connection, and store it on the service object.

        Args:
            addr (str): Connection address. Not used.

        Returns:
            protocol (Protocol): The created protocol.

        """
        self.portal.amp_protocol = AMPServerProtocol()
        self.portal.amp_protocol.factory = self
        return self.portal.amp_protocol


class AMPServerProtocol(amp.AMPMultiConnectionProtocol):
    """
    Protocol subclass for the AMP-server run by the Portal.

    """

    def connectionLost(self, reason):
        """
        Set up a simple callback mechanism to let the amp-server wait for a connection to close.

        """
        callback, args, kwargs = self.factory.disconnect_callbacks.pop(self, (None, None, None))
        if callback:
            try:
                callback(*args, **kwargs)
            except Exception:
                logger.log_trace()

    def start_server(self, server_twistd_cmd):
        """
        (Re-)Launch the Evennia server.

        Args:
            server_twisted_cmd (list): The server start instruction
                to pass to POpen to start the server.

        """
        # start the Server
        try:
            process = Popen(server_twistd_cmd, env=getenv(), bufsize=-1, stdout=PIPE, stderr=STDOUT)
        except Exception:
            self.factory.portal.server_process_id = None
            logger.log_trace()
            return 0
        # there is a short window before the server logger is up where we must
        # catch the stdout of the Server or eventual tracebacks will be lost.
        with process.stdout as out:
            logger.log_server(out.read())

        # store the pid and launch argument for future reference
        self.factory.portal.server_process_id = process.pid
        self.factory.portal.server_twistd_cmd = server_twistd_cmd
        return process.pid

    def wait_for_disconnect(self, callback, *args, **kwargs):
        """
        Add a callback for when this connection is lost.

        Args:
            callback (callable): Will be called with *args, **kwargs
                once this protocol is disconnected.

        """
        self.factory.disconnect_callbacks[self] = (callback, args, kwargs)

    def stop_server(self, mode='shutdown'):
        """
        Shut down server in one or more modes.

        Args:
            mode (str): One of 'shutdown', 'reload' or 'reset'.

        """
        if mode == 'reload':
            self.send_AdminPortal2Server(amp.DUMMYSESSION, operation=amp.SRELOAD)
        elif mode == 'reset':
            self.send_AdminPortal2Server(amp.DUMMYSESSION, operation=amp.SRESET)
        elif mode == 'shutdown':
            self.send_AdminPortal2Server(amp.DUMMYSESSION, operation=amp.SSHUTD)

    # sending amp data

    def send_MsgPortal2Server(self, session, **kwargs):
        """
        Access method called by the Portal and executed on the Portal.

        Args:
            session (session): Session
            kwargs (any, optional): Optional data.

        Returns:
            deferred (Deferred): Asynchronous return.

        """
        return self.data_out(amp.MsgPortal2Server, session.sessid, **kwargs)

    def send_AdminPortal2Server(self, session, operation="", **kwargs):
        """
        Send Admin instructions from the Portal to the Server.
        Executed
        on the Portal.

        Args:
            session (Session): Session.
            operation (char, optional): Identifier for the server operation, as defined by the
                global variables in `evennia/server/amp.py`.
            data (str or dict, optional): Data used in the administrative operation.

        """
        return self.data_out(amp.AdminPortal2Server, session.sessid, operation=operation, **kwargs)

    # receive amp data

    @amp.MsgStatus.responder
    @amp.catch_traceback
    def portal_receive_status(self, status):
        """
        Returns run-status for the server/portal.

        Args:
            status (str): Not used.
        Returns:
            status (dict): The status is a tuple
                (portal_running, server_running, portal_pid, server_pid).

        """
        # check if the server is connected
        server_connected = (self.factory.server_connection and
                            self.factory.server_connection.transport.connected)
        server_pid = self.factory.portal.server_process_id
        portal_pid = os.getpid()

        if server_connected:
            return {"status": amp.dumps((True, True, portal_pid, server_pid))}
        else:
            return {"status": amp.dumps((True, False, portal_pid, server_pid))}

    @amp.MsgLauncher2Portal.responder
    @amp.catch_traceback
    def portal_receive_launcher2portal(self, operation, arguments):
        """
        Receives message arriving from evennia_launcher.
        This method is executed on the Portal.

        Args:
            operation (str): The action to perform.
            arguments (str): Possible argument to the instruction, or the empty string.

        Returns:
            result (dict): The result back to the launcher.

        Notes:
            This is the entrypoint for controlling the entire Evennia system from the evennia
            launcher. It can obviously only accessed when the Portal is already up and running.

        """
        def _retval(success, txt):
            return {"result": amp.dumps((success, txt))}

        server_connected = (self.factory.server_connection and
                            self.factory.server_connection.transport.connected)
        server_pid = self.factory.portal.server_process_id

        logger.log_msg("AMP SERVER operation == %s received" % (ord(operation)))
        logger.log_msg("AMP SERVER arguments: %s" % (amp.loads(arguments)))

        if operation == amp.SSTART:   # portal start  #15
            # first, check if server is already running
            if server_connected:
                return _retval(False,
                               "Server already running at PID={spid}".format(spid=server_pid))
            else:
                spid = self.start_server(amp.loads(arguments))
                return _retval(True, "Server started with PID {spid}.".format(spid=spid))

        elif operation == amp.SRELOAD:  # reload server #14
            if server_connected:
                # don't restart until the server connection goes down
                self.stop_server(mode='reload')
            else:
                spid = self.start_server(amp.loads(arguments))
                return _retval(True, "Server started with PID {spid}.".format(spid=spid))

        elif operation == amp.SRESET:  # reload server #19
            if server_connected:
                self.stop_server(mode='reset')
                return _retval(True, "Server restarted with PID {spid}.".format(spid=spid))
            else:
                spid = self.start_server(amp.loads(arguments))
                return _retval(True, "Server started with PID {spid}.".format(spid=spid))

        elif operation == amp.SSHUTD:  # server-only shutdown #17
            if server_connected:
                self.stop_server(mode='shutdown')
                return _retval(True, "Server stopped.")
            else:
                return _retval(False, "Server not running")

        elif operation == amp.PSHUTD:  # portal + server shutdown  #16
            if server_connected:
                self.stop_server(mode='shutdown')
                return _retval(True, "Server stopped.")
            self.factory.portal.shutdown(restart=False)

        else:
            raise Exception("operation %(op)s not recognized." % {'op': operation})
        # fallback
        return {"result": ""}

    @amp.MsgServer2Portal.responder
    @amp.catch_traceback
    def portal_receive_server2portal(self, packed_data):
        """
        Receives message arriving to Portal from Server.
        This method is executed on the Portal.

        Args:
            packed_data (str): Pickled data (sessid, kwargs) coming over the wire.

        """
        sessid, kwargs = self.data_in(packed_data)
        session = self.factory.portal.sessions.get(sessid, None)
        if session:
            self.factory.portal.sessions.data_out(session, **kwargs)
        return {}

    @amp.AdminServer2Portal.responder
    @amp.catch_traceback
    def portal_receive_adminserver2portal(self, packed_data):
        """

        Receives and handles admin operations sent to the Portal
        This is executed on the Portal.

        Args:
            packed_data (str): Data received, a pickled tuple (sessid, kwargs).

        """
        sessid, kwargs = self.data_in(packed_data)
        operation = kwargs.pop("operation")
        portal_sessionhandler = self.factory.portal.sessions

        # store this transport since we know it comes from the Server
        self.factory.server_connection = self

        if operation == amp.SLOGIN:  # server_session_login
            # a session has authenticated; sync it.
            session = portal_sessionhandler.get(sessid)
            if session:
                portal_sessionhandler.server_logged_in(session, kwargs.get("sessiondata"))

        elif operation == amp.SDISCONN:  # server_session_disconnect
            # the server is ordering to disconnect the session
            session = portal_sessionhandler.get(sessid)
            if session:
                portal_sessionhandler.server_disconnect(session, reason=kwargs.get("reason"))

        elif operation == amp.SDISCONNALL:  # server_session_disconnect_all
            # server orders all sessions to disconnect
            portal_sessionhandler.server_disconnect_all(reason=kwargs.get("reason"))

        elif operation == amp.SRELOAD:  # server reload
                self.factory.server_connection.wait_for_disconnect(
                    self.start_server, self.factory.portal.server_twistd_cmd)
                self.stop_server(mode='reload')

        elif operation == amp.SRESET:  # server reset
                self.factory.server_connection.wait_for_disconnect(
                    self.start_server, self.factory.portal.server_twistd_cmd)
                self.stop_server(mode='reset')

        elif operation == amp.SSHUTD:  # server-only shutdown
            self.stop_server(mode='shutdown')

        elif operation == amp.PSHUTD:  # full server+server shutdown
            self.factory.server_connection.wait_for_disconnect(
                self.factory.portal.shutdown, restart=False)
            self.stop_server(mode='shutdown')

        elif operation == amp.PSYNC:  # portal sync
            # Server has (re-)connected and wants the session data from portal
            sessdata = self.factory.portal.sessions.get_all_sync_data()
            self.send_AdminPortal2Server(amp.DUMMYSESSION,
                                         amp.PSYNC,
                                         sessiondata=sessdata)
            self.factory.portal.sessions.at_server_connection()

        elif operation == amp.SSYNC:  # server_session_sync
            # server wants to save session data to the portal,
            # maybe because it's about to shut down.
            portal_sessionhandler.server_session_sync(kwargs.get("sessiondata"),
                                                      kwargs.get("clean", True))
            # set a flag in case we are about to shut down soon
            self.factory.server_restart_mode = True

        elif operation == amp.SCONN:  # server_force_connection (for irc/etc)
            portal_sessionhandler.server_connect(**kwargs)

        else:
            raise Exception("operation %(op)s not recognized." % {'op': operation})
        return {}
