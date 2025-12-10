#!/usr/bin/env python3
"""
Network Link Monitor - Standalone Test Script
Monitors network interface changes using netlink sockets
"""

import os
import socket
import struct
import logging
import signal
import sys
import time
from datetime import datetime


class NetworkLinkMonitor:
    """Monitor network interface link state changes using netlink sockets."""
    
    # Netlink message types
    NETLINK_ROUTE = 0
    RTMGRP_LINK = 1
    
    def __init__(self, log_file="/var/log/network_monitor.log"):
        self.log_file = log_file
        self._socket = None
        self._running = False
        self.setup_logging()
        
    def setup_logging(self):
        """Setup logging configuration."""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(self.log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def setup_socket(self):
        """Create and bind the netlink socket."""
        try:
            self._socket = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
            self._socket.bind((os.getpid(), self.RTMGRP_LINK))
            self.logger.info(f"Netlink socket created and bound to PID {os.getpid()}")
        except Exception as e:
            self.logger.error(f"Failed to setup socket: {e}")
            raise
            
    def parse_rtm_message(self, data):
        """Parse RTM (Routing Table Message) from netlink data."""
        try:
            # Basic netlink message header parsing
            if len(data) < 16:
                return None
                
            # Extract interface index and message type
            header = struct.unpack("IHHII", data[:16])
            msg_len, msg_type, flags, seq, pid = header
            
            # RTM_NEWLINK = 16, RTM_DELLINK = 17
            if msg_type == 16:
                return "NEWLINK"
            elif msg_type == 17:
                return "DELLINK"
            else:
                return f"UNKNOWN({msg_type})"
        except Exception as e:
            self.logger.error(f"Error parsing RTM message: {e}")
            return None
            
    def monitor_links(self):
        """Main monitoring loop."""
        self.logger.info("Starting network link monitoring...")
        self._running = True
        
        try:
            while self._running:
                try:
                    # Receive netlink messages
                    data, addr = self._socket.recvfrom(8192)
                    
                    if not data:
                        continue
                        
                    msg_type = self.parse_rtm_message(data)
                    if msg_type:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.logger.info(f"Link event: {msg_type} at {timestamp}")
                        
                except socket.timeout:
                    continue
                except Exception as e:
                    self.logger.error(f"Error in monitoring loop: {e}")
                    time.sleep(1)
                    
        except KeyboardInterrupt:
            self.logger.info("Monitoring stopped by user")
        finally:
            self.cleanup()
            
    def cleanup(self):
        """Clean up resources."""
        self._running = False
        if self._socket:
            self._socket.close()
            self.logger.info("Socket closed")
            
    def signal_handler(self, signum, frame):
        """Handle termination signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self._running = False


def main():
    """Main function."""
    # Check if running as root for /var/log access
    if os.geteuid() != 0:
        print("Note: Running as non-root user. Log file will be created in current directory.")
        log_file = "./network_monitor.log"
    else:
        log_file = "/var/log/network_monitor.log"
    
    try:
        monitor = NetworkLinkMonitor(log_file=log_file)
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, monitor.signal_handler)
        signal.signal(signal.SIGTERM, monitor.signal_handler)
        
        # Initialize and start monitoring
        monitor.setup_socket()
        monitor.monitor_links()
        
    except PermissionError:
        print("Error: Permission denied. Try running with sudo for netlink socket access.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()