using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

internal static class HiddenLauncher
{
    private const string ResourceName = "WorkerPayload";

    [STAThread]
    private static int Main(string[] args)
    {
        bool updateMode = args.Length > 0 &&
            String.Equals(args[0], "--integrated-update",
                          StringComparison.OrdinalIgnoreCase);
        try
        {
            return updateMode
                ? RunIntegratedUpdate(RemoveFirstArgument(args))
                : RunWorker();
        }
        catch (Exception error)
        {
            WriteError(error, updateMode);
            return 1;
        }
    }

    private static int RunWorker()
    {
        byte[] payload = ReadPayload();
        string launcher = Assembly.GetExecutingAssembly().Location;
        string appName = Path.GetFileNameWithoutExtension(launcher);
        string binDir = Path.Combine(Path.GetTempPath(), "CodexMeter", "bin");
        Directory.CreateDirectory(binDir);

        string hash = PayloadHash(payload);
        string workerPath = Path.Combine(binDir, appName + "-" + hash + ".exe");
        InstallWorker(workerPath, payload);

        string launcherArgument = Convert.ToBase64String(
            Encoding.UTF8.GetBytes(launcher));
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = workerPath;
        start.Arguments = "--launcher-b64=" + launcherArgument;
        start.WorkingDirectory = binDir;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.WindowStyle = ProcessWindowStyle.Hidden;
        Process.Start(start);
        return 0;
    }

    private static int RunIntegratedUpdate(string[] args)
    {
        Dictionary<string, string> options = ParseArguments(args);
        string source = Path.GetFullPath(Required(options, "--source"));
        string target = Path.GetFullPath(Required(options, "--target"));
        string expectedHash = Required(options, "--sha256").ToLowerInvariant();
        int waitPid = Int32.Parse(Required(options, "--wait-pid"));
        bool restart = options.ContainsKey("--restart");

        WaitForExit(waitPid, TimeSpan.FromSeconds(45));
        if (!File.Exists(source))
            throw new FileNotFoundException("Downloaded client is missing.", source);
        if (!String.Equals(FileHash(source), expectedHash,
                           StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Downloaded client SHA-256 mismatch.");

        string directory = Path.GetDirectoryName(target);
        if (String.IsNullOrEmpty(directory))
            throw new InvalidOperationException("Target directory is invalid.");
        Directory.CreateDirectory(directory);
        string staged = target + ".new";
        string backup = target + ".bak";
        File.Copy(source, staged, true);

        bool replaced = false;
        try
        {
            if (File.Exists(backup))
                File.Delete(backup);
            if (File.Exists(target))
            {
                try
                {
                    File.Replace(staged, target, backup, true);
                }
                catch (PlatformNotSupportedException)
                {
                    File.Copy(target, backup, true);
                    File.Delete(target);
                    File.Move(staged, target);
                }
            }
            else
            {
                File.Move(staged, target);
            }
            replaced = true;

            if (restart)
            {
                ProcessStartInfo start = new ProcessStartInfo();
                start.FileName = target;
                start.WorkingDirectory = directory;
                start.UseShellExecute = true;
                Process.Start(start);
            }
        }
        catch
        {
            if (replaced && File.Exists(backup))
                File.Copy(backup, target, true);
            throw;
        }
        finally
        {
            if (File.Exists(staged))
                File.Delete(staged);
        }
        return 0;
    }

    private static string[] RemoveFirstArgument(string[] args)
    {
        string[] result = new string[Math.Max(0, args.Length - 1)];
        if (result.Length > 0)
            Array.Copy(args, 1, result, 0, result.Length);
        return result;
    }

    private static Dictionary<string, string> ParseArguments(string[] args)
    {
        Dictionary<string, string> values =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (int index = 0; index < args.Length; ++index)
        {
            string key = args[index];
            if (key == "--restart")
            {
                values[key] = "true";
                continue;
            }
            if (!key.StartsWith("--") || index + 1 >= args.Length)
                throw new ArgumentException("Invalid updater arguments.");
            values[key] = args[++index];
        }
        return values;
    }

    private static string Required(
        Dictionary<string, string> values, string key)
    {
        string value;
        if (!values.TryGetValue(key, out value) || String.IsNullOrWhiteSpace(value))
            throw new ArgumentException("Missing updater argument: " + key);
        return value;
    }

    private static void WaitForExit(int pid, TimeSpan timeout)
    {
        try
        {
            Process process = Process.GetProcessById(pid);
            if (!process.WaitForExit((int)timeout.TotalMilliseconds))
                throw new TimeoutException("The running client did not exit.");
        }
        catch (ArgumentException)
        {
        }
    }

    private static string FileHash(string path)
    {
        using (SHA256 sha = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            byte[] digest = sha.ComputeHash(stream);
            StringBuilder text = new StringBuilder(digest.Length * 2);
            foreach (byte value in digest)
                text.Append(value.ToString("x2"));
            return text.ToString();
        }
    }

    private static byte[] ReadPayload()
    {
        using (Stream stream = Assembly.GetExecutingAssembly()
                   .GetManifestResourceStream(ResourceName))
        {
            if (stream == null)
                throw new InvalidOperationException("Embedded sync worker is missing.");
            using (MemoryStream output = new MemoryStream())
            {
                stream.CopyTo(output);
                return output.ToArray();
            }
        }
    }

    private static string PayloadHash(byte[] payload)
    {
        using (SHA256 sha = SHA256.Create())
        {
            byte[] digest = sha.ComputeHash(payload);
            StringBuilder text = new StringBuilder(16);
            for (int i = 0; i < 8; ++i)
                text.Append(digest[i].ToString("x2"));
            return text.ToString();
        }
    }

    private static void InstallWorker(string workerPath, byte[] payload)
    {
        FileInfo existing = new FileInfo(workerPath);
        if (existing.Exists && existing.Length == payload.LongLength)
            return;

        string temporary = workerPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllBytes(temporary, payload);
        try
        {
            File.Move(temporary, workerPath);
        }
        catch (IOException)
        {
            if (!File.Exists(workerPath))
                throw;
            File.Delete(temporary);
        }
    }

    private static void WriteError(Exception error, bool updateMode)
    {
        Console.Error.WriteLine(error);
        try
        {
            string logDir = updateMode
                ? Path.Combine(
                    Environment.GetFolderPath(
                        Environment.SpecialFolder.ApplicationData),
                    "CodexMeter")
                : Path.Combine(Path.GetTempPath(), "CodexMeter");
            Directory.CreateDirectory(logDir);
            File.AppendAllText(
                Path.Combine(
                    logDir,
                    updateMode ? "update-error.log" : "launcher-error.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + error + Environment.NewLine,
                Encoding.UTF8);
        }
        catch
        {
        }
    }
}
