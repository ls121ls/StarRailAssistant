using SRAFrontend.Data;
using SRAFrontend.Desktop.Controls;
using SRAFrontend.Services;

namespace SRAFrontend.Desktop.ViewModels;

public partial class HomePageViewModel(
    ControlPanelViewModel controlPanelViewModel,
    SettingsService settingsService)
    : PageViewModel(PageName.Home, "\uE2C2")
{
    public double GlassCardOpacity => settingsService.Settings.Display.ControlPanelOpacity;
    public ControlPanelViewModel ControlPanelViewModel => controlPanelViewModel;
}
