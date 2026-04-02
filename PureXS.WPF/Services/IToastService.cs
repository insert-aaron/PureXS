namespace PureXS.Services;

public interface IToastService
{
    event EventHandler<ToastItem>? ToastRequested;
    void Show(string message, string level = "info", int durationMs = 3000);
}

public class ToastItem
{
    public string Message { get; init; } = "";
    public string Level { get; init; } = "info";
    public int DurationMs { get; init; } = 3000;
    public DateTime CreatedAt { get; init; } = DateTime.Now;
}
