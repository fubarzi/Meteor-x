#define AppName "METEOR-X"
#define AppVersion "3.2.4"
#define Stage "cache\installer_stage"

[Setup]
AppId={{95092D7A-E139-42F9-B52B-0935A388320A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=METEOR-X
DefaultDirName={localappdata}\Programs\METEOR-X
DefaultGroupName=METEOR-X
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=.
OutputBaseFilename=METEOR-X-3.2.4-Setup-win64
SetupIconFile=_internal\assets\meteor-x.ico
UninstallDisplayIcon={app}\METEOR-X.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#Stage}\METEOR-X.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\meteorrx.ini"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#Stage}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\START-HERE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\drivers\*"; DestDir: "{app}\drivers"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\METEOR-X"; Filename: "{app}\METEOR-X.exe"
Name: "{group}\Uninstall METEOR-X"; Filename: "{uninstallexe}"
Name: "{autodesktop}\METEOR-X"; Filename: "{app}\METEOR-X.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\METEOR-X.exe"; Description: "Launch METEOR-X"; Flags: nowait postinstall skipifsilent
