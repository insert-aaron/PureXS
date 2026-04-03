using System.Windows;

namespace PureXS.Views;

public partial class FacilityTokenDialog : Window
{
    public string? Token { get; private set; }

    public FacilityTokenDialog()
    {
        InitializeComponent();
        TokenInput.Focus();
    }

    private void OK_Click(object sender, RoutedEventArgs e)
    {
        Token = TokenInput.Text.Trim();
        DialogResult = true;
        Close();
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        Token = null;
        DialogResult = false;
        Close();
    }
}
