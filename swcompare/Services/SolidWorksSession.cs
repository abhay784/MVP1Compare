using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace SWCompare.Services;

/// <summary>
/// Singleton wrapper around the ISldWorks COM object.
///
/// SOLIDWORKS COM is single-threaded apartment. We serialise all access through
/// a lock so multiple HTTP requests can't trample each other. For higher
/// throughput we could pool multiple SW instances; that's out of scope for v1.
/// </summary>
public sealed class SolidWorksSession : IDisposable
{
    private readonly object _lock = new();
    private ISldWorks? _swApp;
    private bool _disposed;

    public T WithApp<T>(Func<ISldWorks, T> action)
    {
        lock (_lock)
        {
            EnsureStartedLocked();
            return action(_swApp!);
        }
    }

    private void EnsureStartedLocked()
    {
        if (_swApp != null) return;

        // ProgID for the active version. Could pin to "SldWorks.Application.30"
        // (=2022) etc. if reproducibility across hosts matters more than
        // picking up whatever is installed.
        var type = Type.GetTypeFromProgID("SldWorks.Application")
            ?? throw new InvalidOperationException(
                "SOLIDWORKS not registered on this host (could not resolve ProgID 'SldWorks.Application').");

        var instance = Activator.CreateInstance(type)
            ?? throw new InvalidOperationException("Activator.CreateInstance returned null for SldWorks.Application.");

        _swApp = (ISldWorks)instance;
        _swApp.Visible = false;          // headless
        _swApp.UserControl = false;
    }

    public IModelDoc2 OpenDocument(string path, out string fileKind)
    {
        var ext = Path.GetExtension(path).ToLowerInvariant();
        var docType = ext switch
        {
            ".sldprt" => (int)swDocumentTypes_e.swDocPART,
            ".sldasm" => (int)swDocumentTypes_e.swDocASSEMBLY,
            ".slddrw" => (int)swDocumentTypes_e.swDocDRAWING,
            _ => throw new ArgumentException($"Unsupported file extension: {ext}", nameof(path)),
        };
        fileKind = ext switch
        {
            ".sldprt" => "part",
            ".sldasm" => "assembly",
            ".slddrw" => "drawing",
            _ => "unknown",
        };

        int errors = 0, warnings = 0;
        return WithApp(sw =>
        {
            var doc = sw.OpenDoc6(
                path,
                docType,
                (int)swOpenDocOptions_e.swOpenDocOptions_Silent
                    | (int)swOpenDocOptions_e.swOpenDocOptions_ReadOnly,
                "",
                ref errors,
                ref warnings);
            if (doc == null)
                throw new IOException(
                    $"OpenDoc6 returned null for '{path}'. errors=0x{errors:X} warnings=0x{warnings:X}");
            return doc;
        });
    }

    public void CloseDocument(IModelDoc2 doc)
    {
        WithApp(sw =>
        {
            sw.CloseDoc(doc.GetTitle());
            return 0;
        });
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        if (_swApp != null)
        {
            try
            {
                _swApp.ExitApp();
            }
            catch { /* best-effort shutdown */ }
            finally
            {
                Marshal.FinalReleaseComObject(_swApp);
                _swApp = null;
            }
        }
    }
}
