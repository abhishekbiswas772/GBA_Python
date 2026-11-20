; Inno Setup script for the Windows installer (built by .github/workflows/release.yml).
; iscc /DAppVersion=1.2.3 /DSourceDir=<flet build windows output> /DOutputDir=dist retropy.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\windows"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef AppExe
  #define AppExe "RetroPy.exe"
#endif

[Setup]
AppId={{6E1B3A55-2F4C-4C8E-9D2A-5B7E1C0F9A31}
AppName=RetroPy
AppVersion={#AppVersion}
AppVerName=RetroPy {#AppVersion}
AppPublisher=Abhishek Biswas
AppPublisherURL=https://github.com/abhishekbiswas772/RetroPy
AppSupportURL=https://github.com/abhishekbiswas772/RetroPy/issues
DefaultDirName={autopf}\RetroPy
DefaultGroupName=RetroPy
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=RetroPy-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RetroPy"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall RetroPy"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RetroPy"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,RetroPy}"; Flags: nowait postinstall skipifsilent
