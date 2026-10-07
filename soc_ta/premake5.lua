-- Resolve the engine root for ProjectReference paths.
-- Precedence: --hazel-dir=<path> CLI arg > HAZEL_DIR env var > global HAZEL_DIR > cwd.
-- The editor always passes --hazel-dir, anchored to the running binary, so regenerations
-- stay pinned to the editor's own clone even when multiple clones share HAZEL_DIR.
newoption {
	trigger     = "hazel-dir",
	value       = "PATH",
	description = "Absolute path to the LuckyEngine clone this project belongs to"
}
HazelRootDirectory = _OPTIONS["hazel-dir"] or os.getenv("HAZEL_DIR") or HAZEL_DIR or "."

-- Dev mode: source tree is available, include projects for proper references and rebuild support.
-- Shipped mode: link pre-built DLLs directly.
local isDevBuild = os.isfile(path.join(HazelRootDirectory, "Hazel-ScriptCore", "premake5.lua"))

workspace "SocTa"
	targetdir "build"
	startproject "SocTa"
	configurations { "Debug", "Release", "Dist" }

if isDevBuild then
	group "Hazel"
		include (path.join(HazelRootDirectory, "Hazel", "vendor", "Coral", "Coral.Managed"))
		include (path.join(HazelRootDirectory, "Hazel-ScriptCore"))
	group ""
end

project "SocTa"
	location "Assets/Scripts/Client"
	kind "SharedLib"
	language "C#"
	dotnetframework "net9.0"

	targetname "SocTa"
	targetdir "Assets/Scripts/Binaries"
	objdir "Assets/Scripts/Intermediates"

	vsprops {
		AppendTargetFrameworkToOutputPath = "false",
		Nullable = "enable",
		CopyLocalLockFileAssemblies = "true",
		EnableDynamicLoading = "true",
		RollForward = "Major",
	}

	files {
		"Assets/Scripts/Client/Source/**.cs",
		"Assets/ContentVault/**.cs",
	}

	if isDevBuild then
		links { "Hazel-ScriptCore", "Coral.Managed" }
	else
		links {
			path.join(HazelRootDirectory, "Resources", "Scripts", "Hazel-ScriptCore.dll"),
			path.join(HazelRootDirectory, "Resources", "Scripts", "Coral.Managed.dll")
		}
	end

	filter "Debug"
		optimize "Off"
		symbols "Default"

	filter "Release"
		optimize "On"
		symbols "Default"

	filter "Dist"
		optimize "Full"
		symbols "Off"
