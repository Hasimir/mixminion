# Copyright 2002-2003 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ServerMain.py,v 1.35 2003/02/05 06:45:51 nickm Exp $

"""mixminion.ServerMain

   The main loop and related functionality for a Mixminion server.

   See the "MixminionServer" class for more information about how it
   all works. """

__all__ = [ 'MixminonServer' ]

import fcntl
import getopt
import os
import sys
import signal
import time
import threading
# We pull this from mixminion.Common, just in case somebody still has
# a copy of the old "mixminion/server/Queue.py" (since renamed to
# ServerQueue.py)
from mixminion.Common import MessageQueue

import mixminion.Config
import mixminion.Crypto
import mixminion.server.MMTPServer
import mixminion.server.Modules
import mixminion.server.PacketHandler
import mixminion.server.ServerQueue
import mixminion.server.ServerConfig
import mixminion.server.ServerKeys

from bisect import insort
from mixminion.Common import LOG, LogStream, MixError, MixFatalError, ceilDiv,\
     createPrivateDir, formatBase64, formatTime, installSIGCHLDHandler, \
     secureDelete, waitForChildren

class IncomingQueue(mixminion.server.ServerQueue.Queue):
    """A DeliveryQueue to accept packets from incoming MMTP connections,
       and hold them until they can be processed.  As packets arrive, and
       are stored to disk, we notify a message queue so that another thread
       can read them."""
    def __init__(self, location, packetHandler):
        """Create an IncomingQueue that stores its messages in <location>
           and processes them through <packetHandler>."""
        mixminion.server.ServerQueue.Queue.__init__(self, location, create=1)
        self.packetHandler = packetHandler
        self.mixPool = None
        self.moduleManager = None

    def connectQueues(self, mixPool, manager, processingThread):
        """Sets the target mix queue"""
        self.mixPool = mixPool
        self.moduleManager = manager #XXXX003 refactor.
        self.processingThread = processingThread
        for h in self.getAllMessages():
            assert h is not None
            self.processingThread.addJob(
                lambda self=self, h=h: self.__deliverMessage(h))

    def queueMessage(self, msg):
        """Add a message for delivery"""
        LOG.trace("Inserted message %s into incoming queue",
                  formatBase64(msg[:8]))
        h = mixminion.server.ServerQueue.Queue.queueMessage(self, msg)
        assert h is not None
        self.processingThread.addJob(
            lambda self=self, h=h: self.__deliverMessage(h))

    def __deliverMessage(self, handle):
        """Process a single message with a given handle, and insert it into
           the Mix pool.  This function is called from within the processing
           thread."""
        ph = self.packetHandler
        message = self.messageContents(handle)
        try:
            res = ph.processMessage(message)
            if res is None:
                # Drop padding before it gets to the mix.
                LOG.debug("Padding message %s dropped",
                          formatBase64(message[:8]))
                self.removeMessage(handle)
            else:
                if res.isDelivery():
                    res.decode()
                LOG.debug("Processed message %s; inserting into pool",
                          formatBase64(message[:8]))
                self.mixPool.queueObject(res)
                self.removeMessage(handle)
        except mixminion.Crypto.CryptoError, e:
            LOG.warn("Invalid PK or misencrypted header in message %s: %s",
                     formatBase64(message[:8]), e)
            self.removeMessage(handle)
        except mixminion.Packet.ParseError, e:
            LOG.warn("Malformed message %s dropped: %s",
                     formatBase64(message[:8]), e)
            self.removeMessage(handle)
        except mixminion.server.PacketHandler.ContentError, e:
            LOG.warn("Discarding bad packet %s: %s",
                     formatBase64(message[:8]), e)
            self.removeMessage(handle)
        except:
            LOG.error_exc(sys.exc_info(),
                    "Unexpected error when processing message %s (handle %s)",
                          formatBase64(message[:8]), handle)
            self.removeMessage(handle) # ???? Really dump this message?

class MixPool:
    """Wraps a mixminion.server.Queue.*MixQueue to send messages to an exit
       queue and a delivery queue.  The files in the MixQueue are instances
       of RelayedPacket or DeliveryPacket from PacketHandler.

       All methods on this class are invoked from the main thread.
    """
    def __init__(self, config, queueDir):
        """Create a new MixPool, based on this server's configuration and
           queue location."""

        server = config['Server']
        interval = server['MixInterval'][2]
        if server['MixAlgorithm'] == 'TimedMixQueue':
            self.queue = mixminion.server.ServerQueue.TimedMixQueue(
                location=queueDir, interval=interval)
        elif server['MixAlgorithm'] == 'CottrellMixQueue':
            self.queue = mixminion.server.ServerQueue.CottrellMixQueue(
                location=queueDir, interval=interval,
                minPool=server.get("MixPoolMinSize", 5),
                sendRate=server.get("MixPoolRate", 0.6))
        elif server['MixAlgorithm'] == 'BinomialCottrellMixQueue':
            self.queue = mixminion.server.ServerQueue.BinomialCottrellMixQueue(
                location=queueDir, interval=interval,
                minPool=server.get("MixPoolMinSize", 5),
                sendRate=server.get("MixPoolRate", 0.6))
        else:
            raise MixFatalError("Got impossible mix queue type from config")

        self.outgoingQueue = None
        self.moduleManager = None

    def lock(self):
        self.queue.lock()

    def unlock(self):
        self.queue.unlock()

    def queueObject(self, obj):
        """Insert an object into the queue."""
        obj.isDelivery() #XXXX003 remove this implicit typecheck.
        self.queue.queueObject(obj)

    def count(self):
        "Return the number of messages in the queue"
        return self.queue.count()

    def connectQueues(self, outgoing, manager):
        """Sets the queue for outgoing mixminion packets, and the
           module manager for deliverable messages."""
        self.outgoingQueue = outgoing
        self.moduleManager = manager

    def mix(self):
        """Get a batch of messages, and queue them for delivery as
           appropriate."""
        if self.queue.count() == 0:
            LOG.trace("No messages in the mix pool")
            return
        handles = self.queue.getBatch()
        LOG.debug("%s messages in the mix pool; delivering %s.",
                  self.queue.count(), len(handles))
        
        for h in handles:
            packet = self.queue.getObject(h)
            #XXXX remove the first case after 0.0.3
            if type(packet) == type(()):
                LOG.debug("  (skipping message %s in obsolete format)", h)
            elif packet.isDelivery():
                LOG.debug("  (sending message %s to exit modules)",
                          formatBase64(packet.getContents()[:8]))
                self.moduleManager.queueDecodedMessage(packet)
            else:
                LOG.debug("  (sending message %s to MMTP server)",
                          formatBase64(packet.getPacket()[:8]))
                self.outgoingQueue.queueDeliveryMessage(packet)
            self.queue.removeMessage(h)

    def getNextMixTime(self, now):
        """Given the current time, return the time at which we should next
           mix."""
        return now + self.queue.getInterval()

class OutgoingQueue(mixminion.server.ServerQueue.DeliveryQueue):
    """DeliveryQueue to send messages via outgoing MMTP connections.  All
       methods on this class are called from the main thread.  The underlying
       objects in this queue are instances of RelayedPacket.

       All methods in this class are run from the main thread.
    """
    def __init__(self, location):
        """Create a new OutgoingQueue that stores its messages in a given
           location."""
        mixminion.server.ServerQueue.DeliveryQueue.__init__(self, location)
        self.server = None

    def configure(self, config):
        retry = config['Outgoing/MMTP']['Retry']
        self.setRetrySchedule(retry)

    def connectQueues(self, server):
        """Set the MMTPServer that this OutgoingQueue informs of its
           deliverable messages."""
        self.server = server

    def _deliverMessages(self, msgList):
        "Implementation of abstract method from DeliveryQueue."
        # Map from addr -> [ (handle, msg) ... ]
        msgs = {}
        # XXXX003 SKIP DEAD MESSAGES!!!!
        for handle, packet, n_retries in msgList:
            addr = packet.getAddress()
            message = packet.getPacket()
            msgs.setdefault(addr, []).append( (handle, message) )
        for addr, messages in msgs.items():
            handles, messages = zip(*messages)
            self.server.sendMessages(addr.ip, addr.port, addr.keyinfo,
                                     list(messages), list(handles))

class _MMTPServer(mixminion.server.MMTPServer.MMTPAsyncServer):
    """Implementation of mixminion.server.MMTPServer that knows about
       delivery queues.

       All methods in this class are run from the main thread.
       """
    def __init__(self, config, tls):
        mixminion.server.MMTPServer.MMTPAsyncServer.__init__(self, config, tls)

    def connectQueues(self, incoming, outgoing):
        self.incomingQueue = incoming
        self.outgoingQueue = outgoing

    def onMessageReceived(self, msg):
        self.incomingQueue.queueMessage(msg)

    def onMessageSent(self, msg, handle):
        self.outgoingQueue.deliverySucceeded(handle)

    def onMessageUndeliverable(self, msg, handle, retriable):
        self.outgoingQueue.deliveryFailed(handle, retriable)
#----------------------------------------------------------------------
class CleaningThread(threading.Thread):
    """Thread that handles file deletion.  Some methods of secure deletion
       are slow enough that they'd block the server if we did them in the
       main thread.
    """
    # Fields:
    #   mqueue: A MessageQueue holding filenames to delete, or None to indicate
    #     a shutdown.
    def __init__(self):
        threading.Thread.__init__(self)
        self.mqueue = MessageQueue()

    def deleteFile(self, fname):
        """Schedule the file named 'fname' for deletion"""
        LOG.trace("Scheduling %s for deletion", fname)
        assert fname is not None
        self.mqueue.put(fname)

    def deleteFiles(self, fnames):
        """Schedule all the files in the list 'fnames' for deletion"""
        for f in fnames:
            self.deleteFile(f)

    def shutdown(self):
        """Tell this thread to shut down once it has deleted all pending
           files."""
        LOG.info("Telling cleanup thread to shut down.")
        self.mqueue.put(None)

    def run(self):
        """implementation of the cleaning thread's main loop: waits for
           a filename to delete or an indication to shutdown, then
           acts accordingly."""
        try:
            while 1:
                fn = self.mqueue.get()
                if fn is None:
                    LOG.info("Cleanup thread shutting down.")
                    return
                if os.path.exists(fn):
                    LOG.trace("Deleting %s", fn)
                    secureDelete(fn, blocking=1)
                else:
                    LOG.warn("Delete thread didn't find file %s",fn)
        except:
            LOG.error_exc(sys.exc_info(),
                          "Exception while cleaning; shutting down thread.")

class ProcessingThread(threading.Thread):
    """Background thread to handle CPU-intensive functions."""
    # Fields:
    #   mqueue: a MessageQueue of callable objects.
    
    class _Shutdown:
        def __call__(self):
            raise self

    def __init__(self):
        """Given a MessageQueue object, create a new processing thread."""
        threading.Thread.__init__(self)
        self.mqueue = MessageQueue()

    def shutdown(self):
        LOG.info("Telling processing thread to shut down.")
        self.mqueue.put(ProcessingThread._Shutdown())

    def addJob(self, job):
        """Adds a job to the message queue.  A job is a callable object
           to be invoked by the processing thread.  If the job raises
           ProcessingThread._Shutdown, the processing thread stops running."""
        self.mqueue.put(job)

    def run(self):
        try:
            while 1:
                job = self.mqueue.get()
                job()
        except ProcessingThread._Shutdown:
            LOG.info("Processing thread shutting down.")
            return
        except:
            LOG.error_exc(sys.exc_info(),
                          "Exception while processing; shutting down thread.")

#----------------------------------------------------------------------
STOPPING = 0
def _sigTermHandler(signal_num, _):
    '''(Signal handler for SIGTERM)'''
    signal.signal(signal_num, _sigTermHandler)
    global STOPPING
    STOPPING = 1

GOT_HUP = 0
def _sigHupHandler(signal_num, _):
    '''(Signal handler for SIGTERM)'''
    signal.signal(signal_num, _sigHupHandler)
    global GOT_HUP
    GOT_HUP = 1

def installSignalHandlers():
    """Install signal handlers for sigterm and sighup."""
    signal.signal(signal.SIGHUP, _sigHupHandler)
    signal.signal(signal.SIGTERM, _sigTermHandler)

#----------------------------------------------------------------------

class MixminionServer:
    """Wraps and drives all the queues, and the async net server.  Handles
       all timed events."""
    ## Fields:
    # config: The ServerConfig object for this server
    # keyring: The mixminion.server.ServerKeys.ServerKeyring
    #
    # mmtpServer: Instance of mixminion.ServerMain._MMTPServer.  Receives
    #    and transmits packets from the network.  Places the packets it
    #    receives in self.incomingQueue.
    # incomingQueue: Instance of IncomingQueue.  Holds received packets
    #    before they are decoded.  Decodes packets with PacketHandler,
    #    and places them in mixPool.
    # packetHandler: Instance of PacketHandler.  Used by incomingQueue to
    #    decrypt, check, and re-pad received packets.
    # mixPool: Instance of MixPool.  Holds processed messages, and
    #    periodically decides which ones to deliver, according to some
    #    batching algorithm.
    # moduleManager: Instance of ModuleManager.  Map routing types to
    #    outging queues, and processes non-MMTP exit messages.
    # outgoingQueue: Holds messages waiting to be send via MMTP.
    # DOCDOC cleaningThread, processingthread, incomingQueue
    
    def __init__(self, config):
        """Create a new server from a ServerConfig."""
        LOG.debug("Initializing server")

        self.config = config
        homeDir = config['Server']['Homedir']
        createPrivateDir(homeDir)

        # Lock file.
        # FFFF Refactor this part into common?
        self.lockFile = os.path.join(homeDir, "lock")
        self.lockFD = os.open(self.lockFile, os.O_RDWR|os.O_CREAT, 0600)
        try:
            fcntl.flock(self.lockFD, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except IOError:
            raise MixFatalError("Another server seems to be running.")

        # The pid file.
        self.pidFile = os.path.join(homeDir, "pid")

        self.keyring = mixminion.server.ServerKeys.ServerKeyring(config)
        if self.keyring._getLiveKey() is None:
            LOG.info("Generating a month's worth of keys.")
            LOG.info("(Don't count on this feature in future versions.)")
            # We might not be able to do this, if we password-encrypt keys
            keylife = config['Server']['PublicKeyLifetime'][2]
            nKeys = ceilDiv(30*24*60*60, keylife)
            self.keyring.createKeys(nKeys)

        LOG.debug("Initializing packet handler")
        self.packetHandler = self.keyring.getPacketHandler()
        LOG.debug("Initializing TLS context")
        tlsContext = self.keyring.getTLSContext()
        LOG.debug("Initializing MMTP server")
        self.mmtpServer = _MMTPServer(config, tlsContext)

        # FFFF Modulemanager should know about async so it can patch in if it
        # FFFF needs to.
        LOG.debug("Initializing delivery module")
        self.moduleManager = config.getModuleManager()
        self.moduleManager.configure(config)

        queueDir = os.path.join(homeDir, 'work', 'queues')

        incomingDir = os.path.join(queueDir, "incoming")
        LOG.debug("Initializing incoming queue")
        self.incomingQueue = IncomingQueue(incomingDir, self.packetHandler)
        LOG.debug("Found %d pending messages in incoming queue",
                  self.incomingQueue.count())

        mixDir = os.path.join(queueDir, "mix")

        LOG.trace("Initializing Mix pool")
        self.mixPool = MixPool(config, mixDir)
        LOG.debug("Found %d pending messages in Mix pool",
                       self.mixPool.count())

        outgoingDir = os.path.join(queueDir, "outgoing")
        LOG.debug("Initializing outgoing queue")
        self.outgoingQueue = OutgoingQueue(outgoingDir)
        self.outgoingQueue.configure(config)
        LOG.debug("Found %d pending messages in outgoing queue",
                       self.outgoingQueue.count())

        self.cleaningThread = CleaningThread()
        self.processingThread = ProcessingThread()

        LOG.debug("Connecting queues")
        self.incomingQueue.connectQueues(mixPool=self.mixPool,
                                         manager=self.moduleManager,
                                        processingThread=self.processingThread)
        self.mixPool.connectQueues(outgoing=self.outgoingQueue,
                                   manager=self.moduleManager)
        self.outgoingQueue.connectQueues(server=self.mmtpServer)
        self.mmtpServer.connectQueues(incoming=self.incomingQueue,
                                      outgoing=self.outgoingQueue)

        self.cleaningThread.start()
        self.processingThread.start()
        self.moduleManager.startThreading()

    def run(self):
        """Run the server; don't return unless we hit an exception."""
        global GOT_HUP
        f = open(self.pidFile, 'wt')
        f.write("%s\n" % os.getpid())
        f.close()

        self.cleanQueues()

        # List of (eventTime, eventName) tuples.  Current names are:
        #  'MIX', 'SHRED', and 'TIMEOUT'.  Kept in sorted order.
        scheduledEvents = []
        now = time.time()

        scheduledEvents.append( (now + 600, "SHRED") )#FFFF make configurable
        scheduledEvents.append( (self.mmtpServer.getNextTimeoutTime(now),
                                 "TIMEOUT") )
        nextMix = self.mixPool.getNextMixTime(now)
        scheduledEvents.append( (nextMix, "MIX") )
        LOG.debug("First mix at %s", formatTime(nextMix,1))
        scheduledEvents.sort()

        # FFFF Support for automatic key rotation.
        while 1:
            nextEventTime = scheduledEvents[0][0]
            now = time.time()
            timeLeft = nextEventTime - now
            while timeLeft > 0:
                # Handle pending network events
                self.mmtpServer.process(2)
                if STOPPING:
                    LOG.info("Caught sigterm; shutting down.")
                    return
                elif GOT_HUP:
                    LOG.info("Caught sighup")
                    LOG.info("Resetting logs")
                    LOG.reset()
                    GOT_HUP = 0
                # ???? This could slow us down a good bit.  Move it?
                if not (self.cleaningThread.isAlive() and
                        self.processingThread.isAlive() and
                        self.moduleManager.thread.isAlive()):
                    LOG.fatal("One of our threads has halted; shutting down.")
                    return
                
                # Calculate remaining time.
                now = time.time()
                timeLeft = nextEventTime - now

            event = scheduledEvents[0][1]
            del scheduledEvents[0]

            if event == 'TIMEOUT':
                LOG.trace("Timing out old connections")
                self.mmtpServer.tryTimeout(now)
                insort(scheduledEvents,
                       (self.mmtpServer.getNextTimeoutTime(now), "TIMEOUT"))
            elif event == 'SHRED':
                self.cleanQueues()
                insort(scheduledEvents, (now + 600, "SHRED"))
            elif event == 'MIX':
                # Before we mix, we need to log the hashes to avoid replays.
                # FFFF We need to recover on server failure.

                try:
                    # There's a potential threading problem here... in
                    # between this sync and the 'mix' below, nobody should
                    # insert into the mix pool.
                    self.mixPool.lock()
                    self.packetHandler.syncLogs()

                    LOG.trace("Mix interval elapsed")
                    # Choose a set of outgoing messages; put them in
                    # outgoingqueue and modulemanager
                    self.mixPool.mix()
                finally:
                    self.mixPool.unlock()
                    
                # Send outgoing messages
                self.outgoingQueue.sendReadyMessages()
                # Send exit messages
                self.moduleManager.sendReadyMessages()

                # Choose next mix interval
                nextMix = self.mixPool.getNextMixTime(now)
                insort(scheduledEvents, (nextMix, "MIX"))
                LOG.trace("Next mix at %s", formatTime(nextMix,1))
            else:
                assert event in ("MIX", "SHRED", "TIMEOUT")

    def cleanQueues(self):
        """Remove all deleted messages from queues"""
        LOG.trace("Expunging deleted messages from queues")
        df = self.cleaningThread.deleteFiles
        self.incomingQueue.cleanQueue(df)
        self.mixPool.queue.cleanQueue(df)
        self.outgoingQueue.cleanQueue(df)
        self.moduleManager.cleanQueues(df)

    def close(self):
        """Release all resources; close all files."""
        self.cleaningThread.shutdown()
        self.processingThread.shutdown()
        self.moduleManager.shutdown()

        self.cleaningThread.join()
        self.processingThread.join()
        self.moduleManager.join()
        
        self.packetHandler.close()
        try:
            os.unlink(self.lockFile)
            fcntl.flock(self.lockFD, fcntl.LOCK_UN)
            os.close(self.lockFD)
            os.unlink(self.pidFile)
        except OSError:
            pass

#----------------------------------------------------------------------
def daemonize():
    """Put the server into daemon mode with the standard trickery."""
    # ??? This 'daemonize' logic should go in Common.

    # This logic is more-or-less verbatim from Stevens's _Advanced
    # Programming in the Unix Environment_:

    # Fork, to run in the background.
    pid = os.fork()
    if pid != 0:
        os._exit(0)
    # Call 'setsid' to make ourselves a new session.
    if hasattr(os, 'setsid'):
        # Setsid is not available everywhere.
        os.setsid()
        # Fork again so the parent, (the session group leader), can exit. This
        # means that we, as a non-session group leader, can never regain a
        # controlling terminal.
        pid = os.fork()
        if pid != 0:
            os._exit(0)
    # Chdir to / so that we don't hold the CWD unnecessarily.
    os.chdir(os.path.normpath("/")) # WIN32 Is this right on Windows?
    # Set umask to 000 so that we drop any (possibly nutty) umasks that
    # our users had before.
    os.umask(0000)
    # Close all unused fds.
    # (We could try to do this via sys.stdin.close() etc., but then we
    #  would miss the magic copies in sys.__stdin__, sys.__stdout__, etc.
    #  Using os.close instead just nukes the FD for us.)
    os.close(sys.stdin.fileno())
    os.close(sys.stdout.fileno())
    os.close(sys.stderr.fileno())
    # Override stdout and stderr in case some code tries to use them
    sys.stdout = sys.__stdout__ = LogStream("STDOUT", "WARN")
    sys.stderr = sys.__stderr__ = LogStream("STDERR", "WARN")

_SERVER_USAGE = """\
Usage: %s [options]
Options:
  -h, --help:                Print this usage message and exit.
  -f <file>, --config=<file> Use a configuration file other than the default.
""".strip()

def usageAndExit(cmd):
    print _SERVER_USAGE %cmd
    sys.exit(0)

def configFromServerArgs(cmd, args):
    options, args = getopt.getopt(args, "hf:", ["help", "config="])
    if args:
        usageAndExit(cmd)
    configFile = None
    for o,v in options:
        if o in ('-h', '--help'):
            usageAndExit(cmd)
        if o in ('-f', '--config'):
            configFile = v

    return readConfigFile(configFile)

def readConfigFile(configFile):
    if configFile is None:
        if os.path.exists(os.path.expanduser("~/.mixminiond.conf")):
            configFile = os.path.expanduser("~/.mixminiond.conf")
        elif os.path.exists(os.path.expanduser("~/etc/mixminiond.conf")):
            configFile = os.path.expanduser("~/etc/mixminiond.conf")
        elif os.path.exists("/etc/mixminiond.conf"):
            configFile = "/etc/mixminiond.conf"
        else:
            print >>sys.stderr, "No config file found or specified."
            sys.exit(1)

    try:
        print "Reading configuration from %s"%configFile
        return mixminion.server.ServerConfig.ServerConfig(fname=configFile)
    except (IOError, OSError), e:
        print >>sys.stderr, "Error reading configuration file %r:"%configFile
        print >>sys.stderr, "   ", str(e)
        sys.exit(1)
    except mixminion.Config.ConfigError, e:
        print >>sys.stderr, "Error in configuration file %r"%configFile
        print >>sys.stderr, str(e)
        sys.exit(1)
    return None #suppress pychecker warning

#----------------------------------------------------------------------
def runServer(cmd, args):
    config = configFromServerArgs(cmd, args)
    try:
        mixminion.Common.LOG.configure(config)
        LOG.debug("Configuring server")
    except:
        info = sys.exc_info()
        LOG.fatal_exc(info,"Exception while configuring server")
        LOG.fatal("Shutting down because of exception: %s", info[0])
        #XXXX if sys.stderr is still real, send a message there as well.
        sys.exit(1)

    if config['Server'].get("Daemon",1):
        print "Starting server in the background"
        try:
            daemonize()
        except:
            info = sys.exc_info()
            LOG.fatal_exc(info,
                          "Exception while starting server in the background")
            os._exit(0)

    installSIGCHLDHandler()
    installSignalHandlers()

    try:
        mixminion.Common.configureShredCommand(config)
        mixminion.Crypto.init_crypto(config)

        server = MixminionServer(config)
    except:
        info = sys.exc_info()
        LOG.fatal_exc(info,"Exception while configuring server")
        LOG.fatal("Shutting down because of exception: %s", info[0])
        #XXXX if sys.stderr is still real, send a message there as well.
        sys.exit(1)            
            
    LOG.info("Starting server: Mixminion %s", mixminion.__version__)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except:
        info = sys.exc_info()
        LOG.fatal_exc(info,"Exception while running server")
        LOG.fatal("Shutting down because of exception: %s", info[0])
        #XXXX if sys.stderr is still real, send a message there as well.
    LOG.info("Server shutting down")
    server.close()
    LOG.info("Server is shut down")

    sys.exit(0)

#----------------------------------------------------------------------
_KEYGEN_USAGE = """\
Usage: %s [options]
Options:
  -h, --help:                Print this usage message and exit.
  -f <file>, --config=<file> Use a configuration file other than
                                /etc/mixminiond.conf
  -n <n>, --keys=<n>         Generate <n> new keys. (Defaults to 1.)
""".strip()

def runKeygen(cmd, args):
    options, args = getopt.getopt(args, "hf:n:",
                                  ["help", "config=", "keys="])
    # FFFF password-encrypted keys
    # FFFF Ability to fill gaps
    # FFFF Ability to generate keys with particular start/end intervals
    keys=1
    usage=0
    configFile = None
    for opt,val in options:
        if opt in ('-h', '--help'):
            usage=1
        elif opt in ('-f', '--config'):
            configFile = val
        elif opt in ('-n', '--keys'):
            try:
                keys = int(val)
            except ValueError:
                print >>sys.stderr,("%s requires an integer" %opt)
                usage = 1
    if usage:
        print _KEYGEN_USAGE % cmd
        sys.exit(1)

    config = readConfigFile(configFile)

    LOG.setMinSeverity("INFO")
    mixminion.Crypto.init_crypto(config)
    keyring = mixminion.server.ServerKeys.ServerKeyring(config)
    print "Creating %s keys..." % keys
    for i in xrange(keys):
        keyring.createKeys(1)
        print ".... (%s/%s done)" % (i+1,keys)

#----------------------------------------------------------------------
_REMOVEKEYS_USAGE = """\
Usage: %s [options]
Options:
  -h, --help:                Print this usage message and exit.
  -f <file>, --config=<file> Use a configuration file other than
                                /etc/mixminiond.conf
  --remove-identity          Remove the identity key as well.  (DANGEROUS!)
""".strip()

def removeKeys(cmd, args):
    # FFFF Resist removing keys that have been published.
    # FFFF Generate 'suicide note' for removing identity key.
    options, args = getopt.getopt(args, "hf:", ["help", "config=",
                                                "remove-identity"])
    if args:
        print >>sys.stderr, "%s takes no arguments"%cmd
        usage = 1
        args = options = ()
    usage = 0
    removeIdentity = 0
    configFile = None
    for opt,val in options:
        if opt in ('-h', '--help'):
            usage=1
        elif opt in ('-f', '--config'):
            configFile = val
        elif opt == '--remove-identity':
            removeIdentity = 1
    if usage:
        print _REMOVEKEYS_USAGE % cmd
        sys.exit(0)

    config = readConfigFile(configFile)
    mixminion.Common.configureShredCommand(config)
    LOG.setMinSeverity("INFO")
    keyring = mixminion.server.ServerKeys.ServerKeyring(config)
    keyring.checkKeys()
    # This is impossibly far in the future.
    keyring.removeDeadKeys(now=(1L << 36))
    if removeIdentity:
        keyring.removeIdentityKey()
    LOG.info("Done removing keys")
