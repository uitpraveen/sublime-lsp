import os
import subprocess
import threading
import time
import json
import sublime
import sublime_plugin

from .logger import log
from . import json_helpers
from . import global_vars
from . import lsp_helpers
from . import node_client

# queue module name changed from Python 2 to 3
if int(sublime.version()) < 3000:
    import Queue as queue
else:
    import queue

class LspCommClient(node_client.CommClient):
    __CONTENT_LENGTH_HEADER = b"Content-Length: "

    def __init__(self, script_path):
        self.server_proc = None
        self.script_path = script_path

        # create event handler maps
        self.event_handlers = dict()

        # create response and event queues
        self.msgq = queue.Queue()
        self.eventq = queue.Queue()
        self.asyncReq = {}

        self.debug_proc = None
        self.breakpoints = []

        self.reqType = {}
        self.open_file = open("/Users/mattfs/Desktop/simulate"+str(time.time()*1000.0), "w")

    def makeTimeoutMsg(self, cmd, seq):
        jsonDict = json_helpers.decode(cmd)
        timeoutMsg = {
            "seq": 0,
            "type": "response",
            "success": False,
            "request_seq": seq,
            "command": jsonDict["method"],
            "message": "timeout"
        }
        return timeoutMsg

    def add_event_handler(self, event_name, cb):
        event_handlers = self.event_handlers
        if event_name not in event_handlers:
            event_handlers[event_name] = []
        if cb not in event_handlers[event_name]:
            event_handlers[event_name].append(cb)

    def started(self):
        return self.server_proc is not None

    # work in progress
    def addBreakpoint(self, file, line):
        self.breakpoints.append((file, line))

    # work in progress
    def debug(self, file):
        # TODO: msg if already debugging
        self.debug_proc = subprocess.Popen(["node", "--debug", file],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def sendCmd(self, cmd, cb, seq):
        """
        send single-line command string; no sequence number; wait for response
        this assumes stdin/stdout; for TCP, need to add correlation with sequence numbers
        """
        if self.postCmd(cmd):
            reqSeq = -1
            try:
                while reqSeq < seq:
                    data = self.msgq.get(True, 1)
                    dict = json_helpers.decode(data)
                    reqSeq = dict['request_seq']
                if cb:
                    cb(dict)
            except queue.Empty:
                print("queue timeout")
                if (cb):
                    cb(self.makeTimeoutMsg(cmd, seq))
        else:
            if (cb):
                cb(self.makeTimeoutMsg(cmd, seq))

    def sendCmdAsync(self, cmd, cb, seq):
        """
        Sends the command and registers a callback
        """
        if self.postCmd(cmd):
            self.asyncReq[seq] = cb

    def sendCmdSync(self, cmd, seq):
        """
        Sends the command and wait for the result and returns it
        """
        if self.postCmd(cmd):
            reqSeq = -1
            try:
                while reqSeq < seq:
                    data = self.msgq.get(True, 2)
                    dict = json_helpers.decode(data)
                    reqSeq = dict['request_seq']
                return dict
            except queue.Empty:
                print("queue timeout")
                return self.makeTimeoutMsg(cmd, seq)
        else:
            return self.makeTimeoutMsg(cmd, seq)

    def postCmd(self, cmd):
        """
        Post command to server; no response needed
        """
        log.debug('Posting command: {0}'.format(cmd))
        cmd = lsp_helpers.convert_cmd(cmd)
        if not cmd:
            return False
        self.reqType[cmd["id"]] = cmd["method"]
        cmd = lsp_helpers.format_request(cmd)
        self.open_file.write(cmd)
        self.open_file.flush()
        print("Sending command %s" % cmd)
        if not self.server_proc:
            log.error("can not send request; node process not running")
            return False
        else:
            self.server_proc.stdin.write(cmd.encode())
            self.server_proc.stdin.flush()
            return True

    def getEvent(self):
        """
        Try to get event from event queue
        """
        try:
            ev = self.eventq.get(False)
        except:
            return None
        return ev

    @staticmethod
    def read_msg(stream, msgq, eventq, asyncReq, reqType, proc, asyncEventHandlers):
        """
        Reader thread helper.
        Return True to indicate the wish to stop reading the next message.
        """
        state = "init"
        body_length = 0
        while state != "body":
            header = stream.readline().strip()
            if len(header) == 0:
                if state == 'init':
                    # log.info('0 byte line in stream when expecting header')
                    return proc.poll() is not None
                else:
                    # Done reading header
                    state = "body"
            else:
                state = 'header'
                if header.startswith(LspCommClient.__CONTENT_LENGTH_HEADER):
                    body_length = int(header[len(LspCommClient.__CONTENT_LENGTH_HEADER):])

        if body_length > 0:
            data = stream.read(body_length)
            log.debug('Read body of length: {0}'.format(body_length))
            data_json = data.decode("utf-8")
            data_dict = json_helpers.decode(data_json)
            print("RAW DATA RECEIVED %s" % data_dict)
            print(data_dict.get('result'))
            print(data_dict.get('id'))
            if data_dict.get('result') is not None and data_dict.get("id") is not None:
                msg_id = data_dict['id']
                req_type = reqType.pop(msg_id)
                data_dict = lsp_helpers.convert_response(req_type, data_dict)
                print("CONVERTED TYPE %s" % data_dict)
                if data_dict is None or data_dict["type"] != "response":
                    return False
                log.debug('Body sequence#: {0}'.format(msg_id))
                if msg_id in asyncReq:
                    callback = asyncReq.pop(msg_id, None)
                    if callback:
                        callback(data_dict)
                else:
                    # Only put in the queue if wasn't an async request
                    msgq.put(json_helpers.encode(data_dict))

            else:
                other = lsp_helpers.convert_other(data_dict)
                if not other:
                    return False
                event_name = other["event"]
                if event_name in asyncEventHandlers:
                    for cb in asyncEventHandlers[event_name]:
                        # Run <cb> asynchronously to keep read_msg as small as possible
                        sublime.set_timeout(lambda: cb(other), 0)
                else:
                    eventq.put(data_json)
        else:
            log.info('Body length of 0 in server stream')

        return False

    @staticmethod
    def is_executable(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    @staticmethod
    def which(program):
        fpath, fname = os.path.split(program)
        if fpath:
            if LspCommClient.is_executable(program):
                return program
        else:
            # /usr/local/bin is not on mac default path
            # but is where node is typically installed on mac
            path_list = os.path.expandvars(os.environ["PATH"]) + os.pathsep + "/usr/local/bin" + os.pathsep + os.path.expandvars("$NVM_BIN")
            for path in path_list.split(os.pathsep):
                path = path.strip('"')
                programPath = os.path.join(path, program)
                if LspCommClient.is_executable(programPath):
                    return programPath
        return None


class ServerClient(LspCommClient):

    def __init__(self, script_path):
        """
        Starts a node client (if not already started) and communicate with it.
        The script file to run is passed to the constructor.
        """
        super(ServerClient, self).__init__(script_path)

        # start node process
        pref_settings = sublime.load_settings('Preferences.sublime-settings')
        node_path = pref_settings.get('node_path')
        if node_path:
            print("Path of node executable is configured as: " + node_path)
            configured_node_path = os.path.expandvars(node_path)
            if LspCommClient.is_executable(configured_node_path):
                node_path = configured_node_path
            else:
                node_path = None
                print("Configured node path is not a valid executable.")
        if not node_path:
            if os.name == "nt":
                node_path = "node"
            else:
                node_path = LspCommClient.which("node")
        if not node_path:
            path_list = os.environ["PATH"] + os.pathsep + "/usr/local/bin" + os.pathsep + "$NVM_BIN"
            print("Unable to find executable file for node on path list: " + path_list)
            print("To specify the node executable file name, use the 'node_path' setting")
            self.server_proc = None
        else:
            global_vars._node_path = node_path
            print("Trying to spawn node executable from: " + node_path)
            try:
                if os.name == "nt":
                    # linux subprocess module does not have STARTUPINFO
                    # so only use it if on Windows
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.SW_HIDE | subprocess.STARTF_USESHOWWINDOW
                    self.server_proc = subprocess.Popen(["/Users/mattfs/gopath/bin/langserver-go"],
                                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=si)
                else:
                    log.debug("opening " + node_path + " " + script_path)
                    env = os.environ
                    env["GOPATH"] = "/Users/mattfs/gopath"
                    env["PATH"] = env["PATH"] + ":/usr/local/go/bin"
                    self.server_proc = subprocess.Popen(["/Users/mattfs/gopath/bin/langserver-go"],
                                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
            except:
                self.server_proc = None
        # start reader thread
        if self.server_proc and (not self.server_proc.poll()):
            log.debug("server proc " + str(self.server_proc))
            log.debug("starting reader thread")
            readerThread = threading.Thread(target=ServerClient.__reader, args=(
                self.server_proc.stdout, self.msgq, self.eventq, self.asyncReq, self.reqType, self.server_proc, self.event_handlers))
            readerThread.daemon = True
            readerThread.start()
        self.sendCmdSync(lsp_helpers.init_message("file:///Users/mattfs/gopath/src/sourcegraph.com/sourcegraph/sourcegraph", self.server_proc.pid), 0)

    @staticmethod
    def __reader(stream, msgq, eventq, asyncReq, reqType, proc, eventHandlers):
        """ Main function for reader thread """
        while True:
            if LspCommClient.read_msg(stream, msgq, eventq, asyncReq, reqType, proc, eventHandlers):
                log.debug("server exited")
                return


class WorkerClient(LspCommClient):
    stop_worker = False
    
    def __init__(self, script_path):
        super(WorkerClient, self).__init__(script_path)

    def start(self):
        WorkerClient.stop_worker = False

        node_path = global_vars.get_node_path()
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.SW_HIDE | subprocess.STARTF_USESHOWWINDOW
            self.server_proc = subprocess.Popen(
                [node_path, self.script_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=si
            )
        else:
            self.server_proc = subprocess.Popen(
                [node_path, self.script_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        # start reader thread
        if self.server_proc and (not self.server_proc.poll()):
            log.debug("worker proc " + str(self.server_proc))
            log.debug("starting worker thread")
            workerThread = threading.Thread(target=WorkerClient.__reader, args=(
                self.server_proc.stdout, self.msgq, self.eventq, self.asyncReq, self.reqType, self.server_proc, self.event_handlers))
            workerThread.daemon = True
            workerThread.start()

    def stop(self):
        WorkerClient.stop_worker = True
        self.server_proc.kill()
        self.server_proc = None

    @staticmethod
    def __reader(stream, msgq, eventq, asyncReq, reqType, proc, eventHandlers):
        """ Main function for worker thread """
        while True:
            if LspCommClient.read_msg(stream, msgq, eventq, asyncReq, reqType, proc, eventHandlers) or WorkerClient.stop_worker:
                log.debug("worker exited")
                return