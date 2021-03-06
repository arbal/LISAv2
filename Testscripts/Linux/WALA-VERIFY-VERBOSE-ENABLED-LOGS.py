#!/usr/bin/env python
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache License.
from azuremodules import *

import argparse
import os
import platform
import time
import sys

parser = argparse.ArgumentParser()


def install_and_import(package):
    import importlib
    try:
        importlib.import_module(package)
    except ImportError:
        import pip
        pip.main(['install', '--user', package])
        import site
        importlib.reload(site)
    finally:
        globals()[package] = importlib.import_module(package)


file_path = os.path.dirname(os.path.realpath(__file__))
constants_path = os.path.join(file_path, "constants.sh")
params = GetParams(constants_path)
passwd = params["PASSWORD"]
if sys.version_info[0] >= 3:
    install_and_import('distro')
    distro = distro.linux_distribution(full_distribution_name=False)
else:
    distro = platform.dist()


def RunTest():
    UpdateState("TestRunning")
    if(distro[0].upper() == "COREOS"):
        versionOutPut = Run("waagent --version")
    else:
        output = Run("pgrep -fa python3.*waagent")
        if ("python3" in output) :
            versionOutPut = Run("/usr/bin/python3 /usr/sbin/waagent --version")
        else :
            versionOutPut = Run("/usr/sbin/waagent --version")

    RunLog.info("Checking log waagent.log...")
    if("2.0." in versionOutPut):
        output = Run("grep -i 'iptables -I INPUT -p udp --dport' /var/log/waagent* | wc -l | tr -d '\n'")
        RunLog.info("agent version is 2.0")
    else:
        output = Run("grep -i 'VERBOSE' /var/log/waagent* | wc -l | tr -d '\n'")
        RunLog.info("agent version > 2.0")

    if not (output == "0") :
        RunLog.info('The log file contains the verbose logs')
        ResultLog.info('PASS')
        UpdateState("TestCompleted")
    else :
        RunLog.error('Verify waagent.log fail, the log file does not contain the verbose logs')
        ResultLog.error('FAIL')
        UpdateState("TestCompleted")


def Restartwaagent():
    if (distro[0].upper() == "COREOS"):
        Run("echo '"+passwd+"' | sudo -S sed -i s/Logs.Verbose=n/Logs.Verbose=y/g  /usr/share/oem/waagent.conf")
    elif (DetectDistro()[0] == 'clear-linux-os'):
        Run("echo '"+passwd+"' | sudo -S sed -i s/Logs.Verbose=n/Logs.Verbose=y/g  \
            /usr/share/defaults/waagent/waagent.conf")
    else:
        Run("echo '"+passwd+"' | sudo -S sed -i s/Logs.Verbose=n/Logs.Verbose=y/g  /etc/waagent.conf")
    RunLog.info("Restart waagent service...")
    result = Run("echo '"+passwd+"' | sudo -S find / -name systemctl |wc -l | tr -d '\n'")
    if (distro[0].upper() == "UBUNTU") or (distro[0].upper() == "DEBIAN"):
        Run("echo '"+passwd+"' | sudo -S service walinuxagent restart")
    else:
        if (result == "0") :
            os.system("echo '"+passwd+"' | sudo -S service waagent restart")
        else:
            os.system("echo '"+passwd+"' | sudo -S systemctl restart waagent")
    time.sleep(60)

Restartwaagent()
RunTest()
