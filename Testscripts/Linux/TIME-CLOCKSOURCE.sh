#!/bin/bash
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache License.

########################################################################
#
# Core_Time_Clocksource.sh
#
# Description:
#	This script was created to check and unbind the current clocksource.
#
################################################################
# Source utils.sh
. utils.sh || {
	echo "Error: unable to source utils.sh!"
	echo "TestAborted" > state.txt
	exit 0
}

# Source constants file and initialize most common variables
UtilsInit

#
# Check the file of current_clocksource
#
CheckSource()
{
	current_clocksource="/sys/devices/system/clocksource/clocksource0/current_clocksource"

	# Microsoft LIS installed version has lis_hyperv_clocksource_tsc_page
	# Without Microsoft LIS, hyperv_clocksource_tsc_page
	# CentOS 6.8 or older versions, hyperv_clocksource
	clocksource="hyperv_clocksource_tsc_page"
	mj=$(echo "$DISTRO_VERSION" | cut -d '.' -f 1)
	mn=$(echo "$DISTRO_VERSION" | cut -d '.' -f 2)
	if [[ $DISTRO_NAME == "centos" || $DISTRO_NAME == "rhel" ]] && [[ $mj -lt 7 && $mn -lt 9 ]]; then
		clocksource="hyperv_clocksource"
	fi
	LogMsg "clocksource: $clocksource"

	if ! [[ $(find $current_clocksource -type f -size +0M) ]]; then
		LogErr "Test Failed. No file was found current_clocksource greater than 0M."
		SetTestStateFailed
		exit 0
	else
		__file_content=$(cat $current_clocksource)
		LogMsg "Found currect_clocksource $__file_content in $current_clocksource"
		if [[ $__file_content == *"$clocksource" ]]; then
			LogMsg "Test successful. Proper file was found. Clocksource file content is $__file_content"
		else
			LogErr "Test failed. Proper file was NOT found. Expected $clocksource, found $__file_content"
			SetTestStateFailed
			exit 0
		fi
	fi

	# check cpu with tsc, for Intel CPU shows constant_tsc, for AMD cpu, only tsc
	if grep -q tsc /proc/cpuinfo
	then
		LogMsg "Test successful. /proc/cpuinfo contains flag tsc"
	else
		LogErr "Test failed. /proc/cpuinfo does not contain flag tsc"
		SetTestStateFailed
		exit 0
	fi

	# check dmesg with hyperv_clocksource
	if [[ $(detect_linux_distribution) == clear-linux-os ]]; then
		__dmesg_output=$(dmesg | grep -e "clocksource $clocksource")
	else
		__dmesg_output=$(grep -rnw '/var/log' -e "clocksource $clocksource" --ignore-case)
	fi
	LogMsg "dmesg log search result: $__dmesg_output"
	if [[ -n $__dmesg_output ]]
	then
		LogMsg "Test successful. dmesg contains log - $__dmesg_output"
	else
		LogErr "Test failed. dmesg does not contain log - $__dmesg_output"
		SetTestStateFailed
		exit 0
	fi
}
function UnbindCurrentSource()
{
	unbind_file="/sys/devices/system/clocksource/clocksource0/unbind_clocksource"
	clocksource="hyperv_clocksource_tsc_page"
	if echo $clocksource > $unbind_file
	then
		_clocksource=$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource)
		LogMsg "Found _clocksource: $_clocksource"
		retryTime=1
		maxRetryTimes=20
		while [ $retryTime -le $maxRetryTimes ]
		do
			LogMsg "Sleep 10 seconds for message show up in log file for the $retryTime time(s)."
			sleep 10
			val=$(grep -rnw '/var/log' -e 'Switched to clocksource acpi_pm' --ignore-case)
			if [ -n "$val" ];then
				break
			fi
			retryTime=$(($retryTime+1))
		done

		if [ -n "$val" ] && [ "$_clocksource" == "acpi_pm" ]; then
			LogMsg "Test successful. After unbind, current clocksource is $_clocksource"
		else
			LogErr "Test failed. After unbind, current clocksource is $_clocksource. Expected acpi_pm"
			SetTestStateFailed
			exit 0
		fi
	else
		LogErr "Test failed. Can not unbind $clocksource"
		SetTestStateFailed
		exit 0
	fi
}
#
# MAIN SCRIPT
#
GetDistro
case $DISTRO in
	redhat_6 | centos_6 | redhat_7 | centos_7)
		LogMsg "WARNING: $DISTRO does not support unbind current clocksource, only check"
		CheckSource
		;;
	redhat_8 |centos_8|fedora*|clear-linux-os)
		CheckSource
		UnbindCurrentSource
		;;
	ubuntu*|debian*)
		CheckSource
		UnbindCurrentSource
		;;
	suse* )
		CheckSource
		UnbindCurrentSource
		;;
	coreos* )
		CheckSource
		UnbindCurrentSource
		;;
	*)
		msg="ERROR: Distro '$DISTRO' not supported"
		LogMsg "${msg}"
		SetTestStateFailed
		exit 0
		;;
esac

LogMsg "Test completed successfully."
SetTestStateCompleted
exit 0
