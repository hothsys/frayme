-- Source for Frayme.app. Rebuild with: launcher/build-app.sh
--
-- An AppleScript applet (rather than a plain shell-script bundle) is used so
-- macOS can prompt for Downloads/removable-volume access. The server started
-- here inherits the applet's privacy permissions.
--
-- The running dialog uses dialog.icns (the bare logo on a transparent
-- background) rather than applet.icns, whose rounded-square backdrop shows up
-- as a white box in the dialog.

on run
	set appPath to POSIX path of (path to me)
	set projectDir to do shell script "dirname " & quoted form of appPath

	-- Gatekeeper runs a quarantined app from a temporary read-only copy (App
	-- Translocation), where frayme.sh next to it can't be found
	if appPath contains "/AppTranslocation/" then
		display dialog "Frayme can't find its project folder." & return & return & ¬
			"macOS is running it from a temporary copy because the project was downloaded or copied with a quarantine flag." & return & return & ¬
			"To fix it, open Terminal in the project folder and run:" & return & return & ¬
			"    launcher/build-app.sh" & return & return & ¬
			"Then open the app again." with title "Frayme" buttons {"OK"} default button "OK" with icon caution
		return
	end if
	try
		do shell script "test -f " & quoted form of (projectDir & "/frayme.sh")
	on error
		display dialog "Frayme can't find frayme.sh in:" & return & return & projectDir & return & return & ¬
			"Keep the app in the project folder, next to frayme.sh." with title "Frayme" buttons {"OK"} default button "OK" with icon caution
		return
	end try

	set ctl to quoted form of (projectDir & "/frayme.sh")
	set envPrefix to "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "

	-- Read port from app.conf (defaults to 5901)
	set portStr to "5901"
	try
		set portStr to do shell script "grep '^PORT=' " & quoted form of (projectDir & "/app.conf") & " 2>/dev/null | cut -d= -f2"
	on error
		set portStr to "5901"
	end try
	if portStr is "" then
		set portStr to "5901"
	end if

	try
		set statusText to do shell script envPrefix & ctl & " status"
	on error
		set statusText to ""
	end try

	if statusText is not "" then
		set serverURL to do shell script "echo " & quoted form of statusText & " | grep -o 'http://[^ ]*'"
		set choice to button returned of (display dialog "Frayme is running at " & serverURL with title "Frayme" buttons {"Stop Frayme", "Open in Browser"} default button "Open in Browser" with icon (path to resource "dialog.icns"))
		if choice is "Open in Browser" then
			open location serverURL
		else
			do shell script envPrefix & ctl & " stop"
		end if
		return
	end if

	-- Before trying to start, check if Frayme is already running from
	-- another directory (e.g. a different clone) by checking the port globally.
	set portInUse to false
	try
		do shell script "python3 -c 'import socket,sys;sys.exit(0 if socket.socket().connect_ex((\"127.0.0.1\"," & portStr & "))==0 else 1)'"
		set portInUse to true
	on error
		set portInUse to false
	end try

	if portInUse then
		set serverURL to "http://localhost:" & portStr
		set choice to button returned of (display dialog "Frayme is running at " & serverURL with title "Frayme" buttons {"Stop Frayme", "Open in Browser"} default button "Open in Browser" with icon (path to resource "dialog.icns"))
		if choice is "Open in Browser" then
			open location serverURL
		else
			do shell script "kill $(lsof -ti:" & portStr & " 2>/dev/null) 2>/dev/null || true"
		end if
		return
	end if

	try
		do shell script envPrefix & ctl & " start 2>&1"
	on error errMsg
		display dialog "Frayme failed to start:" & return & return & errMsg with title "Frayme" buttons {"OK"} default button "OK" with icon stop
	end try
end run
