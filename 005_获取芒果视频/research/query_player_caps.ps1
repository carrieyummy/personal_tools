<#
.SYNOPSIS
在独立的 32 位 PowerShell 进程中查询芒果播放器能力，不创建播放器或操作客户端。
.PARAMETER RequestPath
研究主脚本生成的 JSON 请求路径，包含 DLL 路径、已验证导出名、缓冲区大小和模式列表。
.OUTPUTS
向标准输出写入能力查询结果 JSON；成功返回 0，异常返回非零退出码。
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$RequestPath
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([IntPtr]::Size -ne 4) { throw 'The player DLL requires a 32-bit helper process.' }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

/// <summary>声明播放器 DLL 查询所需的 Windows 加载接口与已验证的调用约定。</summary>
public static class PlayerCapsNative {
    /// <summary>加载模块；path 是 DLL 绝对路径，reserved 必须为空，flags 限制依赖搜索；返回句柄。</summary>
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadLibraryExW(string path, IntPtr reserved, uint flags);

    /// <summary>查找导出；module 为模块句柄，name 为完整导出名；返回函数指针。</summary>
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    public static extern IntPtr GetProcAddress(IntPtr module, string name);

    /// <summary>初始化本进程的 SDK 全局状态；无参数和返回值。</summary>
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void Initialize();

    /// <summary>查询能力；mode 为 SDK 播放模式，buffer 为调用方分配的字节缓冲；无返回值。</summary>
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void Query(int mode, IntPtr buffer);
}
'@

$module = [PlayerCapsNative]::LoadLibraryExW($request.dll_path, [IntPtr]::Zero, 0x1100)
if ($module -eq [IntPtr]::Zero) { throw "LoadLibraryEx failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }

if ($request.initialize) {
    $pointer = [PlayerCapsNative]::GetProcAddress($module, $request.initialize_export)
    if ($pointer -eq [IntPtr]::Zero) { throw 'The initialization export was not found.' }
    $initialize = [Runtime.InteropServices.Marshal]::GetDelegateForFunctionPointer($pointer, [PlayerCapsNative+Initialize])
    $initialize.Invoke()
}

$results = @()
foreach ($mode in $request.modes) {
    $entry = [ordered]@{ mode = $mode }
    foreach ($name in @('hdts', 'exdef')) {
        $exportName = $request.($name + '_export')
        $pointer = [PlayerCapsNative]::GetProcAddress($module, $exportName)
        if ($pointer -eq [IntPtr]::Zero) { throw "The $name export was not found." }
        $query = [Runtime.InteropServices.Marshal]::GetDelegateForFunctionPointer($pointer, [PlayerCapsNative+Query])
        $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal($request.buffer_bytes)
        try {
            $zeros = New-Object byte[] $request.buffer_bytes
            [Runtime.InteropServices.Marshal]::Copy($zeros, 0, $buffer, $zeros.Length)
            $query.Invoke([int]$mode, $buffer)
            $entry[$name] = [Runtime.InteropServices.Marshal]::PtrToStringAnsi($buffer)
        }
        finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
        }
    }
    $results += [pscustomobject]$entry
}
Write-Output ($request.output_marker + ($results | ConvertTo-Json -Depth 8 -Compress))
# DLL 的退出清理由隔离进程完成，不调用未验证的卸载接口。
