using System;
using System.ComponentModel;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Platform.Storage;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using SRAFrontend.Data;
using SRAFrontend.Desktop.Controls;
using SRAFrontend.Desktop.Models;
using SRAFrontend.Desktop.Services;
using SRAFrontend.Models;
using SRAFrontend.Services;
using SukiUI.Controls;
using SukiUI.MessageBox;
using SRAFrontend.Localization;

namespace SRAFrontend.Desktop.ViewModels;

public partial class TaskPageViewModel : PageViewModel
{
    private readonly IBackendService _backendService;
    private readonly CacheService _cacheService;
    private readonly CommonModel _commonModel;
    private readonly ConfigService _configService;

    private bool _isAccountTextBoxFocused;
    private TpTask[] _tpTasks = [];

    public TaskPageViewModel(
        CommonModel commonModel,
        ControlPanelViewModel controlPanelViewModel,
        ConfigService configService,
        CacheService cacheService,
        IBackendService backendService,
        CommandPaletteService commandPaletteService) : base(
        PageName.Task, "\uE1BC")
    {
        ControlPanelViewModel = controlPanelViewModel;
        _commonModel = commonModel;
        _configService = configService;
        _cacheService = cacheService;
        _backendService = backendService;
        CurrentConfig = _configService.TasksConfig!;

        _cacheService.Cache.PropertyChanged += OnCachePropertyChanged;

        commandPaletteService.Register(
            new PaletteCommand("cw.importStrategy", "货币战争：导入攻略", PaletteCategory.Tools),
            () => InstallStrategyCommand.Execute(null));
        
        if (Cache.Strategies.Count == 0) _ = RefreshStrategies();
        return;

        void OnCachePropertyChanged(object? _, PropertyChangedEventArgs args)
        {
            if (args.PropertyName != nameof(Cache.CurrentConfigIndex)) return;
            _configService.SwitchConfig(_cacheService.Cache.ConfigNames[_cacheService.Cache.CurrentConfigIndex]);
            CurrentConfig = _configService.TasksConfig!;
        }
    }

    [ObservableProperty]
    [NotifyPropertyChangedFor(
        nameof(CosmicStrifeConfig),
        nameof(MissionAccomplishedConfig),
        nameof(ReceiveRewardsConfig),
        nameof(StartGameConfig),
        nameof(TrailblazePowerConfig),
        nameof(SelectedGardenOfPlentyLevels1Index),
        nameof(SelectedGardenOfPlentyLevels2Index),
        nameof(SelectedPlanarFissureLevelsIndex),
        nameof(SelectedRealmOfTheStrangeLevelsIndex),
        nameof(SelectedStrategyInvestHint),
        nameof(AccountText))]
    private partial TasksConfig CurrentConfig { get; set; }

    [ObservableProperty] public partial bool IsTpTaskAutoDetect { get; set; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(EnableContextMenu))]
    public partial object? SelectedTaskItem { get; set; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CurrentTpTaskLevels), nameof(CurrentTpTaskMaxSingleTimes))]
    public partial int SelectedTpTaskIndex { get; set; }

    public int SelectedGardenOfPlentyLevels1Index
    {
        get => TrailblazePowerConfig.GardenOfPlentyLevel1;
        set
        {
            TrailblazePowerConfig.GardenOfPlentyLevel1 = value;
            OnPropertyChanged();
        }
    }

    public int SelectedGardenOfPlentyLevels2Index
    {
        get => TrailblazePowerConfig.GardenOfPlentyLevel2;
        set
        {
            TrailblazePowerConfig.GardenOfPlentyLevel2 = value;
            OnPropertyChanged();
        }
    }

    public int SelectedPlanarFissureLevelsIndex
    {
        get => TrailblazePowerConfig.PlanarFissureLevel;
        set
        {
            TrailblazePowerConfig.PlanarFissureLevel = value;
            OnPropertyChanged();
        }
    }

    public int SelectedRealmOfTheStrangeLevelsIndex
    {
        get => TrailblazePowerConfig.RealmOfTheStrangeLevel;
        set
        {
            TrailblazePowerConfig.RealmOfTheStrangeLevel = value;
            OnPropertyChanged();
        }
    }

    [ObservableProperty] public partial TpTaskLevel? SelectedTpTaskLevel { get; set; }

    [ObservableProperty] public partial int TpTaskRunTimes { get; set; } = 1;

    [ObservableProperty] public partial int TpTaskSingleTimes { get; set; } = 1;

    // 副本与关卡名称已由后端按界面语言返回，这里只拼接展示格式
    public string[] TpTaskNames => [.. _tpTasks.Select(t => t.Name)];

    // _tpTasks 在后端就绪前为空，以下取值一律用 ElementAtOrDefault 兜底，避免绑定求值时抛越界异常
    public TpTaskLevel[] CurrentTpTaskLevels => _tpTasks.ElementAtOrDefault(SelectedTpTaskIndex)?.Levels ?? [];

    private static string LevelLabel(TpTaskLevel level) =>
        System.Globalization.CultureInfo.CurrentUICulture.Name == "zh-CN"
            ? $"{level.Name}（{level.Result}）"
            : $"{level.Name} ({level.Result})";

    // 下标对应 tasks/config/trailblaze_power.toml 中 subtasks 的定义顺序
    public string[] GardenOfPlentyLevels1 =>
        [Resources.NotSelectedText, .. (_tpTasks.ElementAtOrDefault(1)?.Levels ?? []).Select(LevelLabel)];

    public string[] GardenOfPlentyLevels2 =>
        [Resources.NotSelectedText, .. (_tpTasks.ElementAtOrDefault(2)?.Levels ?? []).Select(LevelLabel)];

    public string[] PlanarFissureLevels =>
        [Resources.NotSelectedText, .. (_tpTasks.ElementAtOrDefault(0)?.Levels ?? []).Select(LevelLabel)];

    public string[] RealmOfTheStrangeLevels =>
        [Resources.NotSelectedText, .. (_tpTasks.ElementAtOrDefault(4)?.Levels ?? []).Select(LevelLabel)];

    public int CurrentTpTaskMaxSingleTimes => _tpTasks.ElementAtOrDefault(SelectedTpTaskIndex)?.MaxSingleTimes ?? 1;

    // 任务清单里存的是历史名称，展示时按副本 id 重新取当前语言名称
    private string TaskListItemName(TrailblazePowerTaskItem item) =>
        _tpTasks.FirstOrDefault(t => t.Id == item.Id)?.Name ?? item.Name;

    public string TaskListText =>
        TrailblazePowerConfig.TaskList.Count == 0
            ? Resources.NoTasksMessage
            : string.Format(Resources.Culture, Resources.TaskSummaryFormat, string.Join(Resources.ListSeparatorText, TrailblazePowerConfig.TaskList.Select(TaskListItemName).Take(3)), TrailblazePowerConfig.TaskList.Count);

    public CosmicStrifeConfig CosmicStrifeConfig => CurrentConfig.CosmicStrife;
    public MissionAccomplishedConfig MissionAccomplishedConfig => CurrentConfig.MissionAccomplished;
    public ReceiveRewardsConfig ReceiveRewardsConfig => CurrentConfig.ReceiveRewards;
    public StartGameConfig StartGameConfig => CurrentConfig.StartGame;
    public TrailblazePowerConfig TrailblazePowerConfig => CurrentConfig.TrailblazePower;

    public string AccountText
    {
        get => _isAccountTextBoxFocused
            ? StartGameConfig.Username
            : $"{StartGameConfig.Username[..Math.Min(3, StartGameConfig.Username.Length)]}****{StartGameConfig.Username[^Math.Min(3, StartGameConfig.Username.Length)..]}";
        set
        {
            StartGameConfig.Username = value;
            OnPropertyChanged();
        }
    }

    public int CurrencyWarsStrategyIndex
    {
        get => CosmicStrifeConfig.CurrencyWarsStrategyIndex;
        set
        {
            var previous = CosmicStrifeConfig.CurrencyWarsStrategyIndex >= 0
                ? Cache.Strategies.ElementAtOrDefault(CosmicStrifeConfig.CurrencyWarsStrategyIndex)
                : null;
            var strategy = value >= 0 ? Cache.Strategies.ElementAtOrDefault(value) : null;
            CosmicStrifeConfig.CurrencyWarsStrategyIndex = value;
            OnPropertyChanged();
            CosmicStrifeConfig.CurrencyWarsStrategy = strategy?.FileName ?? "";
            // 选择攻略时自动带入其推荐投资环境（多个候选以空格分隔，? 前缀表示可选）；
            // 输入框为空或仍是上一份攻略带入的内容时才覆盖，避免冲掉手动填写
            var environments = strategy is { RecommendEnvironments.Count: > 0 }
                ? string.Join(" ", strategy.RecommendEnvironments)
                : "";
            var previousEnvironments = previous is { RecommendEnvironments.Count: > 0 }
                ? string.Join(" ", previous.RecommendEnvironments)
                : "";
            if (environments.Length > 0 &&
                CanAutoFill(CosmicStrifeConfig.CurrencyWarsRerollInvestEnvironments, previousEnvironments))
                CosmicStrifeConfig.CurrencyWarsRerollInvestEnvironments = environments;
            // 选择攻略时自动带入其推荐投资策略（攻略按优先级分号分隔，这里合并为同一阶段的候选表）
            var investStrategies = BuildInvestStrategyText(strategy?.RecommendInvestStrategies);
            if (investStrategies.Length > 0 &&
                CanAutoFill(CosmicStrifeConfig.CurrencyWarsRerollInvestStrategies,
                    BuildInvestStrategyText(previous?.RecommendInvestStrategies)))
                CosmicStrifeConfig.CurrencyWarsRerollInvestStrategies = investStrategies;
            OnPropertyChanged(nameof(SelectedStrategyInvestHint));
        }
    }

    /// <summary>输入框为空或仍是上一份攻略带入的内容时，才允许自动带入，避免覆盖手动填写。</summary>
    private static bool CanAutoFill(string current, string previousAuto)
        => current.Length == 0 || current == previousAuto;

    /// <summary>把攻略里按优先级排列的投资策略（分号分隔）转成配置格式：同一阶段内用 | 分隔候选，越靠左越优先。</summary>
    private static string BuildInvestStrategyText(string? recommend)
    {
        if (string.IsNullOrWhiteSpace(recommend)) return "";
        var names = recommend
            .Split(new[] { ';', '；', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(name => name.Trim().Replace(" ", ""))
            .Where(name => name.Length > 0)
            .Distinct();
        return string.Join("|", names);
    }

    /// <summary>投资策略输入框的格式说明</summary>
    public string SelectedStrategyInvestHint
    {
        get
        {
            var recommend = Cache.Strategies
                .ElementAtOrDefault(CosmicStrifeConfig.CurrencyWarsStrategyIndex)?.RecommendInvestStrategies;
            return string.IsNullOrWhiteSpace(recommend)
                ? "格式：| 分隔＝任一命中即可（越靠左越优先）；; 分隔＝按阶段依次指定。"
                : "已按攻略带入（越靠左越优先）；| 分隔＝任一命中即可，; 分隔＝按阶段依次指定。";
        }
    }

    public ControlPanelViewModel ControlPanelViewModel { get; }

    public bool EnableContextMenu => SelectedTaskItem is not null;

    public int CurrencyWarsModeIndex
    {
        get => CosmicStrifeConfig.CurrencyWarsMode;
        set
        {
            CosmicStrifeConfig.CurrencyWarsMode = value;
            OnPropertyChanged(nameof(IsCwNormalMode));
        }
    }

    public bool IsCwNormalMode => CosmicStrifeConfig.CurrencyWarsMode != 2;

    public Cache Cache => _cacheService.Cache;

    public void ToggleAccountTextFocus(bool isFocused)
    {
        _isAccountTextBoxFocused = isFocused;
        OnPropertyChanged(nameof(AccountText));
    }

    public async Task GetTpConfigAsync()
    {
        if (_tpTasks.Length > 0) return;

        try
        {
            // 页面加载时后端可能尚未就绪，命令无响应会返回空数组，
            // 此处重试约 3 秒覆盖 Python 后端冷启动，避免副本列表一直为空
            for (var attempt = 1; attempt <= 5; attempt++)
            {
                _tpTasks = await _backendService.GetTpConfigAsync();
                if (_tpTasks.Length > 0) break;
                if (attempt < 5) await Task.Delay(TimeSpan.FromMilliseconds(800));
            }
        }
        catch (Exception ex)
        {
            _commonModel.ShowErrorToast(Resources.StageConfigLoadFailedError, ex.Message);
            return;
        }

        if (_tpTasks.Length == 0)
            _commonModel.ShowErrorToast(Resources.StageConfigLoadFailedError, Resources.StageConfigBackendErrorMessage);

        OnPropertyChanged(nameof(TpTaskNames));
        OnPropertyChanged(nameof(CurrentTpTaskLevels));
        OnPropertyChanged(nameof(GardenOfPlentyLevels1));
        OnPropertyChanged(nameof(GardenOfPlentyLevels2));
        OnPropertyChanged(nameof(PlanarFissureLevels));
        OnPropertyChanged(nameof(RealmOfTheStrangeLevels));
        OnPropertyChanged(nameof(CurrentTpTaskMaxSingleTimes));
    }

    [RelayCommand]
    private async Task SingleTask(string taskName)
    {
        await ControlPanelViewModel.StartSingleTask(taskName);
    }

    [RelayCommand]
    private async Task RefreshStrategies()
    {
        try
        {
            var strategies = await _backendService.GetStrategiesAsync();
            Cache.Strategies.Clear();
            foreach (var strategy in strategies)
                Cache.Strategies.Add(strategy);
            CurrencyWarsStrategyIndex = 0;
        }
        catch (Exception ex)
        {
            _commonModel.ShowErrorToast(Resources.StrategyLoadFailedError, ex.Message);
        }
    }

    [RelayCommand]
    private async Task InstallStrategy()
    {
        if (App.TopLevelObject is null) return;
        var files = await App.TopLevelObject.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = Resources.SelectStrategyFileText,
            AllowMultiple = false,
            FileTypeFilter = [new FilePickerFileType(Resources.JsonStrategyFileText) { Patterns = ["*.json"] }]
        });
        if (files.Count == 0) return;

        try
        {
            var result = await _backendService.InstallStrategyAsync(files[0].Path.LocalPath);
            if (result is not { Success: true })
            {
                _commonModel.ShowErrorToast(Resources.StrategyImportFailedError, result?.Message ?? Resources.BackendNotRespondingError);
                return;
            }
            _commonModel.ShowSuccessToast(Resources.StrategyImportedMessage, result.Message);
            await RefreshStrategies();
        }
        catch (Exception ex)
        {
            _commonModel.ShowErrorToast(Resources.StrategyImportFailedError, ex.Message);
        }
    }

    [RelayCommand]
    private async Task SelectedPath()
    {
        if (App.TopLevelObject is null) return;
        var files = await App.TopLevelObject.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions());
        if (files.Count == 0) return;
        StartGameConfig.GamePath = files[0].Path.LocalPath;
    }

    [RelayCommand]
    private void DeleteSelectedTaskItem()
    {
        if (SelectedTaskItem is TrailblazePowerTaskItem item) TrailblazePowerConfig.TaskList.Remove(item);
    }

    [RelayCommand]
    private async Task ShowTaskListControl()
    {
        var taskListControl = new TpTaskListControl
        {
            DataContext = this
        };
        await SukiMessageBox.ShowDialog(new SukiMessageBoxHost
        {
            Content = taskListControl
        });
        OnPropertyChanged(nameof(TaskListText)); // 窗口关闭时更新显示文本
    }

    [RelayCommand]
    private async Task ShowAddTaskControl()
    {
        var result = await SukiMessageBox.ShowDialog(new SukiMessageBoxHost
        {
            Content = new TpAddTaskControl { DataContext = this },
            ActionButtonsPreset = SukiMessageBoxButtons.ApplyCancel
        });
        if (result is SukiMessageBoxResult.Apply) AddTaskItem();
    }

    private void AddTaskItem()
    {
        if (SelectedTpTaskLevel is null)
        {
            _commonModel.ShowInfoToast(Resources.InfoText, Resources.StageRequiredMessage);
            return;
        }

        TrailblazePowerConfig.TaskList.Add(new TrailblazePowerTaskItem
        {
            Name = _tpTasks[SelectedTpTaskIndex].Name,
            Id = _tpTasks[SelectedTpTaskIndex].Id,
            Level = SelectedTpTaskLevel.Id,
            LevelName = SelectedTpTaskLevel.Name,
            Count = TpTaskSingleTimes,
            RunTimes = TpTaskRunTimes,
            AutoDetect = IsTpTaskAutoDetect
        });
    }
}
