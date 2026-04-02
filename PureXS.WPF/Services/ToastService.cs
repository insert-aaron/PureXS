namespace PureXS.Services;

public class ToastService : IToastService
{
    public event EventHandler<ToastItem>? ToastRequested;

    public void Show(string message, string level = "info", int durationMs = 3000)
    {
        var item = new ToastItem
        {
            Message = message,
            Level = level,
            DurationMs = durationMs,
            CreatedAt = DateTime.Now
        };
        ToastRequested?.Invoke(this, item);
    }
}
