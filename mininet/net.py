"""

    Mininet: A simple networking testbed for OpenFlow!

author: Bob Lantz (rlantz@cs.stanford.edu)
author: Brandon Heller (brandonh@stanford.edu)

Mininet creates scalable OpenFlow test networks by using
process-based virtualization and network namespaces.

Simulated hosts are created as processes in separate network
namespaces. This allows a complete OpenFlow network to be simulated on
top of a single Linux kernel.

Each host has:

A virtual console (pipes to a shell)
A virtual interfaces (half of a veth pair)
A parent shell (and possibly some child processes) in a namespace

Hosts have a network interface which is configured via ifconfig/ip
link/etc.

This version supports both the kernel and user space datapaths
from the OpenFlow reference implementation (openflowswitch.org)
as well as OpenVSwitch (openvswitch.org.)

In kernel datapath mode, the controller and switches are simply
processes in the root namespace.

Kernel OpenFlow datapaths are instantiated using dpctl(8), and are
attached to the one side of a veth pair; the other side resides in the
host namespace. In this mode, switch processes can simply connect to the
controller via the loopback interface.

In user datapath mode, the controller and switches can be full-service
nodes that live in their own network namespaces and have management
interfaces and IP addresses on a control network (e.g. 10.0.123.1,
currently routed although it could be bridged.)

In addition to a management interface, user mode switches also have
several switch interfaces, halves of veth pairs whose other halves
reside in the host nodes that the switches are connected to.

Consistent, straightforward naming is important in order to easily
identify hosts, switches and controllers, both from the CLI and
from program code. Interfaces are named to make it easy to identify
which interfaces belong to which node.

The basic naming scheme is as follows:

    Host nodes are named h1-hN
    Switch nodes are named s1-sN
    Controller nodes are named c0-cN
    Interfaces are named {nodename}-eth0 .. {nodename}-ethN

Note: If the network topology is created using mininet.topo, then
node numbers are unique among hosts and switches (e.g. we have
h1..hN and SN..SN+M) and also correspond to their default IP addresses
of 10.x.y.z/8 where x.y.z is the base-256 representation of N for
hN. This mapping allows easy determination of a node's IP
address from its name, e.g. h1 -> 10.0.0.1, h257 -> 10.0.1.1.

Currently we wrap the entire network in a 'mininet' object, which
constructs a simulated network based on a network topology created
using a topology object (e.g. LinearTopo) from mininet.topo or
mininet.topolib, and a Controller which the switches will connect
to. Several configuration options are provided for functions such as
automatically setting MAC addresses, populating the ARP table, or
even running a set of xterms to allow direct interaction with nodes.

After the network is created, it can be started using start(), and a
variety of useful tasks maybe performed, including basic connectivity
and bandwidth tests and running the mininet CLI.

Once the network is up and running, test code can easily get access
to host and switch objects which can then be used for arbitrary
experiments, typically involving running a series of commands on the
hosts.

After all desired tests or activities have been completed, the stop()
method may be called to shut down the network.

"""

import os
import re
import signal
from time import sleep

from mininet.cli import CLI
from mininet.log import info, error, debug, cliinfo
from mininet.node import Host, UserSwitch, KernelSwitch, Controller
from mininet.node import ControllerParams
from mininet.util import quietRun, fixLimits
from mininet.util import createLink, macColonHex, ipStr, ipParse
from mininet.xterm import cleanUpScreens, makeXterms

DATAPATHS = [ 'kernel' ] #[ 'user', 'kernel' ]

def init():
    "Initialize Mininet."
    if os.getuid() != 0:
        # Note: this script must be run as root
        # Perhaps we should do so automatically!
        print "*** Mininet must run as root."
        exit( 1 )
    # If which produces no output, then netns is not in the path.
    # May want to loosen this to handle netns in the current dir.
    if not quietRun( [ 'which', 'netns' ] ):
        raise Exception( "Could not find netns; see INSTALL" )
    fixLimits()

class Mininet( object ):
    "Network emulation with hosts spawned in network namespaces."

    def __init__( self, topo, switch=KernelSwitch, host=Host,
                 controller=Controller,
                 cparams=ControllerParams( '10.0.0.0', 8 ),
                 build=True, xterms=False, cleanup=False,
                 inNamespace=False,
                 autoSetMacs=False, autoStaticArp=False ):
        """Create Mininet object.
           topo: Topo (topology) object or None
           switch: Switch class
           host: Host class
           controller: Controller class
           cparams: ControllerParams object
           now: build now from topo?
           xterms: if build now, spawn xterms?
           cleanup: if build now, cleanup before creating?
           inNamespace: spawn switches and controller in net namespaces?
           autoSetMacs: set MAC addrs from topo?
           autoStaticArp: set all-pairs static MAC addrs?"""
        self.switch = switch
        self.host = host
        self.controller = controller
        self.cparams = cparams
        self.topo = topo
        self.inNamespace = inNamespace
        self.xterms = xterms
        self.cleanup = cleanup
        self.autoSetMacs = autoSetMacs
        self.autoStaticArp = autoStaticArp

        self.hosts = []
        self.switches = []
        self.controllers = []
        self.nameToNode = {} # name to Node (Host/Switch) objects
        self.idToNode = {} # dpid to Node (Host/Switch) objects
        self.dps = 0 # number of created kernel datapaths
        self.terms = [] # list of spawned xterm processes

        if build:
            self.build()

    def addHost( self, name, mac=None, ip=None ):
        """Add host.
           name: name of host to add
           mac: default MAC address for intf 0
           ip: default IP address for intf 0
           returns: added host"""
        host = self.host( name, defaultMAC=mac, defaultIP=ip )
        self.hosts.append( host )
        self.nameToNode[ name ] = host
        return host

    def addSwitch( self, name, mac=None, ip=None ):
        """Add switch.
           name: name of switch to add
           mac: default MAC address for kernel/OVS switch intf 0
           returns: added switch"""
        if self.switch == UserSwitch:
            sw = self.switch( name, defaultMAC=mac, defaultIP=ip,
                inNamespace=self.inNamespace )
        else:
            sw = self.switch( name, defaultMAC=mac, defaultIP=ip, dp=self.dps,
                inNamespace=self.inNamespace )
        self.dps += 1
        self.switches.append( sw )
        self.nameToNode[ name ] = sw
        return sw

    def addController( self, controller ):
        """Add controller.
           controller: Controller class"""
        controller = self.controller( 'c0', self.inNamespace )
        if controller: # allow controller-less setups
            self.controllers.append( controller )
            self.nameToNode[ 'c0' ] = controller

    # Control network support:
    #
    # Create an explicit control network. Currently this is only
    # used by the user datapath configuration.
    #
    # Notes:
    #
    # 1. If the controller and switches are in the same (e.g. root)
    #    namespace, they can just use the loopback connection.
    #
    # 2. If we can get unix domain sockets to work, we can use them
    #    instead of an explicit control network.
    #
    # 3. Instead of routing, we could bridge or use 'in-band' control.
    #
    # 4. Even if we dispense with this in general, it could still be
    #    useful for people who wish to simulate a separate control
    #    network (since real networks may need one!)

    def configureControlNetwork( self ):
        "Configure control network."
        self.configureRoutedControlNetwork()

    # We still need to figure out the right way to pass
    # in the control network location.

    def configureRoutedControlNetwork( self, ip='192.168.123.1',
        prefixLen=16 ):
        """Configure a routed control network on controller and switches.
           For use with the user datapath only right now.
           """
        controller = self.controllers[ 0 ]
        info( controller.name + ' <->' )
        cip = ip
        snum = ipParse( ip )
        for switch in self.switches:
            info( ' ' + switch.name )
            sintf, cintf = createLink( switch, controller )
            snum += 1
            while snum & 0xff in [ 0, 255 ]:
                snum += 1
            sip = ipStr( snum )
            controller.setIP( cintf, cip, prefixLen )
            switch.setIP( sintf, sip, prefixLen )
            controller.setHostRoute( sip, cintf )
            switch.setHostRoute( cip, sintf )
        info( '\n' )
        info( '*** Testing control network\n' )
        while not controller.intfIsUp( cintf ):
            info( '*** Waiting for', cintf, 'to come up\n' )
            sleep( 1 )
        for switch in self.switches:
            while not switch.intfIsUp( sintf ):
                info( '*** Waiting for', sintf, 'to come up\n' )
                sleep( 1 )
            if self.ping( hosts=[ switch, controller ] ) != 0:
                error( '*** Error: control network test failed\n' )
                exit( 1 )
        info( '\n' )

    def configHosts( self ):
        "Configure a set of hosts."
        # params were: hosts, ips
        for host in self.hosts:
            hintf = host.intfs[ 0 ]
            host.setIP( hintf, host.defaultIP, self.cparams.prefixLen )
            host.setDefaultRoute( hintf )
            # You're low priority, dude!
            quietRun( 'renice +18 -p ' + repr( host.pid ) )
            info( host.name + ' ' )
        info( '\n' )

    def buildFromTopo( self, topo ):
        """Build mininet from a topology object
           At the end of this function, everything should be connected
           and up."""

        def addNode( prefix, addMethod, nodeId ):
            "Add a host or a switch."
            name = prefix + topo.name( nodeId )
            mac = macColonHex( nodeId ) if self.setMacs else None
            ip = topo.ip( nodeId )
            node = addMethod( name, mac=mac, ip=ip )
            self.idToNode[ nodeId ] = node
            info( name + ' ' )

        # Possibly we should clean up here and/or validate
        # the topo
        if self.cleanup:
            pass

        info( '*** Adding controller\n' )
        self.addController( self.controller )
        info( '*** Creating network\n' )
        info( '*** Adding hosts:\n' )
        for hostId in sorted( topo.hosts() ):
            addNode( 'h', self.addHost, hostId )
        info( '\n*** Adding switches:\n' )
        for switchId in sorted( topo.switches() ):
            addNode( 's', self.addSwitch, switchId )
        info( '\n*** Adding edges:\n' )
        for srcId, dstId in sorted( topo.edges() ):
            src, dst = self.idToNode[ srcId ], self.idToNode[ dstId ]
            srcPort, dstPort = topo.port( srcId, dstId )
            createLink( src, dst, srcPort, dstPort )
            info( '(%s, %s) ' % ( src.name, dst.name ) )
        info( '\n' )

    def build( self ):
        "Build mininet."
        if self.topo:
            self.buildFromTopo( self.topo )
        if self.inNamespace:
            info( '*** Configuring control network\n' )
            self.configureControlNetwork()
        info( '*** Configuring hosts\n' )
        self.configHosts()
        if self.xterms:
            self.startXterms()
        if self.autoSetMacs:
            self.setMacs()
        if self.autoStaticArp:
            self.staticArp()

    def startXterms( self ):
        "Start an xterm for each node."
        info( "*** Running xterms on %s\n" % os.environ[ 'DISPLAY' ] )
        cleanUpScreens()
        self.terms += makeXterms( self.controllers, 'controller' )
        self.terms += makeXterms( self.switches, 'switch' )
        self.terms += makeXterms( self.hosts, 'host' )

    def stopXterms( self ):
        "Kill each xterm."
        # Kill xterms
        for term in self.terms:
            os.kill( term.pid, signal.SIGKILL )
        cleanUpScreens()

    def setMacs( self ):
        """Set MAC addrs to correspond to default MACs on hosts.
           Assume that the host only has one interface."""
        for host in self.hosts:
            host.setMAC( host.intfs[ 0 ], host.defaultMAC )

    def staticArp( self ):
        "Add all-pairs ARP entries to remove the need to handle broadcast."
        for src in self.hosts:
            for dst in self.hosts:
                if src != dst:
                    src.setARP( ip=dst.IP(), mac=dst.MAC() )

    def start( self ):
        "Start controller and switches"
        info( '*** Starting controller\n' )
        for controller in self.controllers:
            controller.start()
        info( '*** Starting %s switches\n' % len( self.switches ) )
        for switch in self.switches:
            info( switch.name + ' ')
            switch.start( self.controllers )
        info( '\n' )

    def stop( self ):
        "Stop the controller(s), switches and hosts"
        if self.terms:
            info( '*** Stopping %i terms\n' % len( self.terms ) )
            self.stopXterms()
        info( '*** Stopping %i hosts\n' % len( self.hosts ) )
        for host in self.hosts:
            info( '%s ' % host.name )
            host.terminate()
        info( '\n' )
        info( '*** Stopping %i switches\n' % len( self.switches ) )
        for switch in self.switches:
            info( switch.name )
            switch.stop()
        info( '\n' )
        info( '*** Stopping %i controllers\n' % len( self.controllers ) )
        for controller in self.controllers:
            controller.stop()
        info( '*** Test complete\n' )

    def run( self, test, **params ):
        "Perform a complete start/test/stop cycle."
        self.start()
        info( '*** Running test\n' )
        result = getattr( self, test )( **params )
        self.stop()
        return result

    @staticmethod
    def _parsePing( pingOutput ):
        "Parse ping output and return packets sent, received."
        r = r'(\d+) packets transmitted, (\d+) received'
        m = re.search( r, pingOutput )
        if m == None:
            error( '*** Error: could not parse ping output: %s\n' %
                     pingOutput )
            exit( 1 )
        sent, received = int( m.group( 1 ) ), int( m.group( 2 ) )
        return sent, received

    def ping( self, hosts=None ):
        """Ping between all specified hosts.
           hosts: list of hosts
           returns: ploss packet loss percentage"""
        # should we check if running?
        packets = 0
        lost = 0
        ploss = None
        if not hosts:
            hosts = self.hosts
            cliinfo( '*** Ping: testing ping reachability\n' )
        for node in hosts:
            cliinfo( '%s -> ' % node.name )
            for dest in hosts:
                if node != dest:
                    result = node.cmd( 'ping -c1 ' + dest.IP() )
                    sent, received = self._parsePing( result )
                    packets += sent
                    if received > sent:
                        error( '*** Error: received too many packets' )
                        error( '%s' % result )
                        node.cmdPrint( 'route' )
                        exit( 1 )
                    lost += sent - received
                    cliinfo( ( '%s ' % dest.name ) if received else 'X ' )
            cliinfo( '\n' )
            ploss = 100 * lost / packets
        cliinfo( "*** Results: %i%% dropped (%d/%d lost)\n" %
                ( ploss, lost, packets ) )
        return ploss

    def pingAll( self ):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        return self.ping()

    def pingPair( self ):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        hosts = [ self.hosts[ 0 ], self.hosts[ 1 ] ]
        return self.ping( hosts=hosts )

    @staticmethod
    def _parseIperf( iperfOutput ):
        """Parse iperf output and return bandwidth.
           iperfOutput: string
           returns: result string"""
        r = r'([\d\.]+ \w+/sec)'
        m = re.search( r, iperfOutput )
        if m:
            return m.group( 1 )
        else:
            raise Exception( 'could not parse iperf output: ' + iperfOutput )

    def iperf( self, hosts=None, l4Type='TCP', udpBw='10M' ):
        """Run iperf between two hosts.
           hosts: list of hosts; if None, uses opposite hosts
           l4Type: string, one of [ TCP, UDP ]
           returns: results two-element array of server and client speeds"""
        if not hosts:
            hosts = [ self.hosts[ 0 ], self.hosts[ -1 ] ]
        else:
            assert len( hosts ) == 2
        host0, host1 = hosts
        cliinfo( '*** Iperf: testing ' + l4Type + ' bandwidth between ' )
        cliinfo( "%s and %s\n" % ( host0.name, host1.name ) )
        host0.cmd( 'killall -9 iperf' )
        iperfArgs = 'iperf '
        bwArgs = ''
        if l4Type == 'UDP':
            iperfArgs += '-u '
            bwArgs = '-b ' + udpBw + ' '
        elif l4Type != 'TCP':
            raise Exception( 'Unexpected l4 type: %s' % l4Type )
        server = host0.cmd( iperfArgs + '-s &' )
        debug( '%s\n' % server )
        client = host1.cmd( iperfArgs + '-t 5 -c ' + host0.IP() + ' ' +
                           bwArgs )
        debug( '%s\n' % client )
        server = host0.cmd( 'killall -9 iperf' )
        debug( '%s\n' % server )
        result = [ self._parseIperf( server ), self._parseIperf( client ) ]
        if l4Type == 'UDP':
            result.insert( 0, udpBw )
        cliinfo( '*** Results: %s\n' % result )
        return result

    def iperfUdp( self, udpBw='10M' ):
        "Run iperf UDP test."
        return self.iperf( l4Type='UDP', udpBw=udpBw )

    def interact( self ):
        "Start network and run our simple CLI."
        self.start()
        result = CLI( self )
        self.stop()
        return result